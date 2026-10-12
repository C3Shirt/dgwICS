# -*- coding: utf-8 -*-
"""One-shot CODESYS ScriptEngine bridge, compatible with IronPython 2.7.

The request is passed through FACTORYIO_CODESYS_REQUEST, never scriptargs.
Only a project copied by the host runner is opened. Device preparation accepts
an explicit source-hash-bound patch; deployment uses a separately verified template.

API references: content.helpme-codesys.com/en/ScriptingEngine/
ScriptTextualObject.html, ScriptApplication.html, ScriptSystem.html,
ScriptOnline.html, ScriptDeviceParameters.html, ScriptDeviceObject.html.
The MIT mcptoolkit-for-codesys project was consulted for the SP22 Build message
category GUID and active_application wrapper behavior. No source was copied.
"""
from __future__ import print_function

import hashlib
import io
import json
import os
import re
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET
from copy import deepcopy

try:
    text_type = unicode
except NameError:
    text_type = str

_BUILD_CATEGORY = "97f48d64-a2a3-4856-b640-75c046e37ea9"
_MAX_NODES = 3000
_MAX_PARAMETERS = 5000
_TASK_TYPE = "98a2708a-9b18-4f31-82ed-a1465b24fa2d"
_TASK_REFERENCE_TYPE = "413e2a7d-adb1-4d2c-be29-6ae6e4fab820"


def _category_guid(value):
    """Real ScriptEngine overloads require System.Guid; CPython mocks use text."""
    try:
        from System import Guid
    except ImportError:
        return value
    return Guid(_text(value).strip("{}"))


def _text(value):
    if value is None:
        return ""
    try:
        return text_type(value)
    except Exception:
        return repr(value)


def _hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path, value):
    if os.path.lexists(path):
        raise RuntimeError("Refusing to overwrite existing bridge artifact: " + path)
    descriptor, temporary = tempfile.mkstemp(prefix=".codesys-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with io.open(descriptor, "w", encoding="utf-8") as stream:
            stream.write(_text(json.dumps(value, ensure_ascii=True, indent=2)) + "\n")
        os.rename(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _name(obj):
    try:
        return _text(obj.get_name())
    except Exception:
        return _text(getattr(obj, "name", "<unnamed>"))


def _children(obj):
    return list(obj.get_children(False))


def _flag(obj, attribute):
    try:
        return bool(getattr(obj, attribute))
    except Exception:
        return False


def _walk(project):
    entries, pending = [], [(project, "", 0)]
    while pending:
        node, parent_path, depth = pending.pop()
        if depth > 50 or len(entries) >= _MAX_NODES:
            raise RuntimeError("Project tree exceeds inspection bounds")
        path = parent_path + "/" + _name(node) if parent_path else _name(node)
        entries.append((node, path))
        for child in reversed(_children(node)):
            pending.append((child, path, depth + 1))
    return entries


def _document(obj, attribute):
    try:
        document = getattr(obj, attribute)
        text = _text(document.text)
        if len(text) > 200000:
            return {"truncated": True, "characters": len(text), "prefix": text[:200000]}
        return text
    except Exception:
        return None


def _properties(obj, names):
    result = {}
    for name in names:
        try:
            result[name] = _text(getattr(obj, name))
        except Exception:
            pass
    return result


def _parameters(device, device_path, budget):
    result = []
    connectors = getattr(device, "connectors", [])
    for connector in connectors:
        connector_id = _text(getattr(connector, "connector_id", ""))
        pending = [(p, _text(getattr(p, "name", getattr(p, "id", ""))), 0, None)
                   for p in getattr(connector, "host_parameters", [])]
        while pending:
            parameter, path, depth, inherited_channel_type = pending.pop(0)
            if budget[0] >= _MAX_PARAMETERS or depth > 20:
                raise RuntimeError("Device parameter inspection exceeds bounds")
            budget[0] += 1
            row = _properties(parameter, ("id", "name", "value", "iec_type", "type_string", "channel_type", "bit_size"))
            if row.get("channel_type") not in ("Input", "Output") and inherited_channel_type in ("Input", "Output"):
                row["channel_type"] = inherited_channel_type
                row["channel_type_inherited"] = True
            row.update({"device_path": device_path, "connector_id": connector_id, "parameter_path": path,
                        "is_mappable_io": _flag(parameter, "is_mappable_io")})
            if row["is_mappable_io"]:
                mapping = getattr(parameter, "io_mapping", None)
                row["mapping"] = _properties(mapping, ("variable", "default_variable", "manual_iec_address")) if mapping is not None else None
                if mapping is not None:
                    row["mapping"]["maps_to_existing_variable"] = _flag(mapping, "maps_to_existing_variable")
            result.append(row)
            if _flag(parameter, "has_sub_elements"):
                for child in parameter:
                    name = _text(getattr(child, "name", getattr(child, "id", "")))
                    pending.append((child, path + "/" + name, depth + 1, row.get("channel_type")))
    return result


def _inspect(project):
    result = {"schema_version": 1, "object_tree": [], "device_mappings": [], "tasks": [], "diagnostics": []}
    budget = [0]
    for obj, path in _walk(project):
        entry = {"name": _name(obj), "path": path, "type": _text(getattr(obj, "type", "")),
                 "is_application": _flag(obj, "is_application"), "is_device": _flag(obj, "is_device"),
                 "is_folder": _flag(obj, "is_folder")}
        for field in ("textual_declaration", "textual_implementation"):
            if _flag(obj, "has_" + field):
                entry[field] = _document(obj, field)
        result["object_tree"].append(entry)
        if entry["is_device"]:
            try:
                result["device_mappings"].extend(_parameters(obj, path, budget))
                settings = obj.get_device_communication_settings()
                entry["communication"] = _properties(settings, ("gateway", "device_name", "device_address", "tracking_mode"))
            except Exception as exc:
                result["diagnostics"].append({"code": "device_inspection", "path": path, "message": _text(exc)})
        # Task extension availability varies by scripting version. Read only.
        task = _properties(obj, ("interval", "priority", "task_type"))
        if task or _flag(obj, "is_task") or entry["type"].lower() == _TASK_TYPE:
            task.update({"name": _name(obj), "path": path, "children": [_name(ch) for ch in _children(obj)]})
            task["pou_calls"] = [_name(ch) for ch in _children(obj)
                                 if _text(getattr(ch, "type", "")).lower() == _TASK_REFERENCE_TYPE]
            if not task.get("interval") or not task.get("priority"):
                result["diagnostics"].append({"code": "task_inspection", "path": path,
                                               "message": "Task interval or priority could not be read"})
            try:
                task["watchdog"] = _properties(obj.watchdog, ("enabled", "time", "time_unit", "sensitivity"))
            except Exception:
                pass
            result["tasks"].append(task)
    return result


def _qualified_variable(value, application_name):
    if not isinstance(value, text_type) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){1,2}$", value):
        raise RuntimeError("Device binding variable must name its POU/GVL")
    parts = value.split(".")
    if len(parts) == 3:
        if parts[0].lower() != application_name.lower():
            raise RuntimeError("Device binding names a different Application")
        return value.lower()
    return (application_name + "." + value).lower()


def _verify_device_bindings(inspection, expected, bindings, application_name):
    """Match each expected endpoint against the current project's actual channels."""
    report = {"passed": False, "matched": [], "diagnostics": []}
    try:
        if inspection.get("diagnostics"):
            raise RuntimeError("Project inspection has incomplete device/task API reads")
        if not isinstance(expected, list) or not expected or not isinstance(bindings, list):
            raise RuntimeError("Explicit verified device bindings are required")
        fields = set(("tag_key", "variable", "device_path", "connector_id", "manual_iec_address", "channel_type", "bit_size"))
        by_tag = dict((b["tag_key"], b) for b in bindings)
        if len(by_tag) != len(bindings):
            raise RuntimeError("Duplicate program binding keys")
        seen_keys, seen_variables, seen_endpoints = set(), set(), set()
        actual_rows = inspection.get("device_mappings")
        if not isinstance(actual_rows, list):
            raise RuntimeError("Actual device mapping inspection is missing")
        for row in expected:
            if not isinstance(row, dict) or set(row) != fields:
                raise RuntimeError("Malformed verified device binding row")
            key = row["tag_key"]
            if key not in by_tag or key in seen_keys:
                raise RuntimeError("Unknown or duplicate verified tag_key")
            variable = _qualified_variable(row["variable"], application_name)
            if variable != _qualified_variable(by_tag[key]["variable"], application_name) or variable in seen_variables:
                raise RuntimeError("Verified variable differs or is duplicated")
            endpoint = (row["device_path"], row["connector_id"], row["manual_iec_address"])
            if endpoint in seen_endpoints:
                raise RuntimeError("Duplicate verified device endpoint")
            candidates, variable_rows = [], []
            for actual in actual_rows:
                mapping = actual.get("mapping") or {}
                if actual.get("is_mappable_io") is not True:
                    continue
                if ((actual.get("device_path"), actual.get("connector_id"), mapping.get("manual_iec_address")) == endpoint
                        and actual.get("channel_type") == row["channel_type"] and _text(actual.get("bit_size")) == _text(row["bit_size"])):
                    candidates.append(actual)
                mapped_variable = mapping.get("variable")
                if mapped_variable:
                    try:
                        if _qualified_variable(mapped_variable, application_name) == variable:
                            variable_rows.append(actual)
                    except RuntimeError:
                        pass
            if len(candidates) != 1 or len(variable_rows) != 1 or candidates[0] is not variable_rows[0]:
                raise RuntimeError("Expected channel/variable is missing, duplicated or mapped elsewhere: " + key)
            actual = candidates[0]
            mapping = actual.get("mapping") or {}
            if mapping.get("maps_to_existing_variable") is not True:
                raise RuntimeError("Channel does not map to a declared existing variable: " + key)
            if actual.get("channel_type") != row["channel_type"] or _text(actual.get("bit_size")) != _text(row["bit_size"]):
                raise RuntimeError("Actual channel direction or width differs: " + key)
            seen_keys.add(key); seen_variables.add(variable); seen_endpoints.add(endpoint)
            report["matched"].append(dict(row))
        if seen_keys != set(by_tag):
            raise RuntimeError("Verified device bindings do not cover every program binding")
        report["passed"] = True
    except Exception as exc:
        report["diagnostics"].append({"code": "device_bindings_unverified", "message": _text(exc)})
    return report


def _verify_task_program(inspection, application_name, program_name):
    report = {"passed": False, "task_paths": [], "diagnostics": []}
    if inspection.get("diagnostics"):
        report["diagnostics"].append({"code": "task_program_unverified", "message": "Project inspection has incomplete API reads"})
        return report
    applications = [row for row in inspection.get("object_tree", [])
                    if row.get("is_application") is True and row.get("name") == application_name]
    if len(applications) == 1:
        prefix = applications[0]["path"] + "/"
        for task in inspection.get("tasks", []):
            if task.get("path", "").startswith(prefix) and any(_text(call).lower() == program_name.lower() for call in task.get("pou_calls", [])):
                report["task_paths"].append(task["path"])
    report["passed"] = bool(report["task_paths"])
    if not report["passed"]:
        report["diagnostics"].append({"code": "task_program_unverified", "message": "No actual task reference calls the expected program in the selected Application"})
    return report


def _device_elements(device, connector_id):
    connectors = [c for c in device.connectors if _text(c.connector_id) == connector_id]
    if len(connectors) != 1:
        raise RuntimeError("Expected exactly one connector for device patch")
    roots = list(connectors[0].host_parameters)
    elements, pending = [], [(p, 0) for p in roots]
    while pending:
        element, depth = pending.pop()
        if depth > 30 or len(elements) >= _MAX_PARAMETERS:
            raise RuntimeError("Device patch exceeds element bounds")
        elements.append(element)
        if _flag(element, "has_sub_elements"):
            pending.extend((child, depth + 1) for child in element)
    return roots, elements


def _resize_native_output_bits(source_path, destination_path):
    """Observed SP22/Modbus 4.6 format: extend one output BYTE to two.

    The supported ScriptParameter.value setter updates count values without
    changing the serialized channel array. Native import is verified again via
    the public parameter inspection and compiler; this is not a general SDK.
    """
    tree = ET.parse(source_path)
    root = tree.getroot()
    def parameter(id_):
        matches = [e for e in root.iter() if e.find("Single[@Name='Id']") is not None and e.find("Single[@Name='Id']").text == id_]
        if len(matches) != 1:
            raise RuntimeError("Native bit resize parameter is missing/ambiguous: " + id_)
        return matches[0]
    input_, output_ = parameter("3000"), parameter("4000")
    if input_.find("Single[@Name='ParamType']").text != "std:ARRAY[0..1] OF BYTE" or output_.find("Single[@Name='ParamType']").text != "std:ARRAY[0..0] OF BYTE":
        raise RuntimeError("Native bit resize only supports inspected two-input-byte/one-output-byte template")
    data = output_.find("Single[@Name='DalaElement']")
    array = data.find("Single[@Name='SubElements']/List2")
    if len(array) != 1 or data.find("Single[@Name='Dimenstion1LowerBorder']").text != "0" or data.find("Single[@Name='Dimenstion1UpperBorder']").text != "0":
        raise RuntimeError("Native output array differs from observed shape")
    clone = deepcopy(array[0])
    next_position = max(int(e.text) for e in root.iter() if e.get("Name") in ("PositionId", "EditorPositionId")) + 1
    for parent in clone.iter():
        for element in list(parent):
            if element.get("Name") == "IoMapping":
                parent.remove(element)
    for element in clone.iter():
        if element.get("Name") == "Identifier" and element.text and element.text.startswith("4000_0_"):
            element.text = element.text.replace("4000_0_", "4000_1_", 1)
        if element.get("Name") in ("PositionId", "EditorPositionId"):
            element.text = str(next_position); next_position += 1
    array.append(clone)
    data.find("Single[@Name='Dimenstion1UpperBorder']").text = "1"
    data.find("Single[@Name='LmArrayTypeGuid']").text = input_.find("Single[@Name='DalaElement']/Single[@Name='LmArrayTypeGuid']").text
    output_.find("Single[@Name='ParamType']").text = "std:ARRAY[0..1] OF BYTE"
    if os.path.lexists(destination_path):
        raise RuntimeError("Native resize destination already exists")
    tree.write(destination_path, encoding="utf-8", xml_declaration=True)


def _import_resized_device(project, device, request):
    identity = device.get_device_identification()
    if _text(identity.type) != "115" or _text(identity.id) != "0000 0002" or _text(identity.version) != "4.6.0.0":
        raise RuntimeError("Native bit resize is unverified for this device version")
    directory = os.path.dirname(request["result_path"])
    source = os.path.join(directory, "device-resize-source.export")
    destination = os.path.join(directory, "device-resized.export")
    project.export_native([device], source, recursive=True)
    _resize_native_output_bits(source, destination)
    expected_name = _name(device)
    class Handler(globals()["NativeImportHandler"]):
        def __init__(self): self.failures = []
        def conflict(self, name, existingObject, newguid):
            if name != expected_name or _name(existingObject) != expected_name:
                raise RuntimeError("Native resize attempted to replace another object")
            return globals()["NativeImportResolve"].replace
        def progress(self, name, pastedObject, exception):
            if exception is not None: self.failures.append(_text(exception))
        def skipped(self, name):
            self.failures.append("Native import skipped object: " + _text(name))
    handler = Handler()
    device_path = request["device_patch"]["device_path"]
    parents = [obj for obj, path in _walk(project) if path == device_path.rsplit("/", 1)[0]]
    if len(parents) != 1: raise RuntimeError("Native import parent is missing/ambiguous")
    # Imported device exports are inserted under the parent. Remove only the
    # copied target after its complete export is preserved, avoiding a renamed
    # second server and shifted automatic IEC addresses.
    device.remove()
    outcome = parents[0].import_native(filenames=[destination], handler=handler)
    if _text(outcome).lower() != "ok" or handler.failures:
        raise RuntimeError("Native resized device import failed: " + _text(outcome) + " " + _text(handler.failures))
    return {"source": source, "resized": destination, "source_sha256": _hash(source),
            "resized_sha256": _hash(destination), "import_result": _text(outcome)}


def _apply_device_patch(project, request):
    patch = request.get("device_patch")
    if patch is None:
        return None
    if request.get("deploy"):
        raise RuntimeError("Device preparation cannot deploy in the same run")
    if not isinstance(patch, dict) or set(patch) != set(("source_sha256", "device_path", "connector_id", "parameters", "bindings", "clear_existing_mappings")):
        raise RuntimeError("Invalid device patch schema")
    if patch["source_sha256"] != request["source_sha256"]:
        raise RuntimeError("Device patch was prepared against another source hash")
    targets = [obj for obj, path in _walk(project) if path == patch["device_path"] and _flag(obj, "is_device")]
    if len(targets) != 1:
        raise RuntimeError("Device patch target is missing or ambiguous")
    roots, elements = _device_elements(targets[0], patch["connector_id"])
    project.export_native([targets[0]], os.path.join(os.path.dirname(request["result_path"]), "device-source.export"), recursive=True)
    changes, ids = [], set()
    if not isinstance(patch["parameters"], list) or not isinstance(patch["bindings"], list) or patch["clear_existing_mappings"] is not True:
        raise RuntimeError("Device patch arrays and explicit clear flag are required")
    for row in patch["parameters"]:
        if not isinstance(row, dict) or set(row) != set(("id", "name", "expected_value", "value")) or row["id"] in ids:
            raise RuntimeError("Invalid or duplicate device parameter patch")
        candidates = [p for p in roots if _text(getattr(p, "id", "")) == _text(row["id"]) and _text(getattr(p, "name", "")) == row["name"]]
        if len(candidates) != 1 or _flag(candidates[0], "is_mappable_io") or _flag(candidates[0], "has_sub_elements"):
            raise RuntimeError("Parameter ID/name is not one observed scalar configuration parameter")
        parameter = candidates[0]
        if _text(parameter.value) != row["expected_value"]:
            raise RuntimeError("Device parameter has changed since inspection: " + row["name"])
        changes.append((parameter, row)); ids.add(row["id"])
    # Preflight existing endpoints. Resizing can create additional bit channels;
    # those are checked against the resulting device before assignment.
    bindings = patch["bindings"]
    endpoints, variables = set(), set()
    for row in bindings:
        endpoint = row["manual_iec_address"]
        variable = _qualified_variable(row["variable"], request["application_name"])
        matches = [e for e in elements if _flag(e, "is_mappable_io") and _text(e.io_mapping.manual_iec_address) == endpoint and _text(e.bit_size) == _text(row["bit_size"])]
        if row["device_path"] != patch["device_path"] or row["connector_id"] != patch["connector_id"] or len(matches) > 1:
            raise RuntimeError("Device patch endpoint differs from observed channel: " + endpoint)
        if endpoint in endpoints or variable in variables:
            raise RuntimeError("Duplicate device patch endpoint/variable")
        endpoints.add(endpoint); variables.add(variable)
    for parameter, row in changes:
        parameter.value = row["value"]
        if _text(parameter.value) != row["value"]:
            raise RuntimeError("Device parameter write did not stick: " + row["name"])
    native_resize = None
    if any(row["manual_iec_address"].startswith("%QX") and
           not any(_flag(e, "is_mappable_io") and _text(e.io_mapping.manual_iec_address) == row["manual_iec_address"] and _text(e.bit_size) == _text(row["bit_size"]) for e in elements)
           for row in bindings):
        native_resize = _import_resized_device(project, targets[0], request)
        targets = [obj for obj, path in _walk(project) if path == patch["device_path"] and _flag(obj, "is_device")]
        if len(targets) != 1: raise RuntimeError("Imported device path changed")
    roots, elements = _device_elements(targets[0], patch["connector_id"])
    for old_parameter, row in changes:
        matches = [p for p in roots if _text(getattr(p, "id", "")) == _text(row["id"])]
        if len(matches) != 1 or _text(matches[0].value) != row["value"]:
            raise RuntimeError("Device update lost patched parameter: " + row["name"])
    _write_json(os.path.join(os.path.dirname(request["result_path"]), "device-resize-inspection.json"), _inspect(project))
    roots, elements = _device_elements(targets[0], patch["connector_id"])
    for element in elements:
        if _flag(element, "is_mappable_io") and _text(element.io_mapping.variable):
            element.io_mapping.variable = ""
    for row in bindings:
        matches = [e for e in elements if _flag(e, "is_mappable_io") and _text(e.io_mapping.manual_iec_address) == row["manual_iec_address"] and _text(e.bit_size) == _text(row["bit_size"])]
        if len(matches) != 1:
            raise RuntimeError("Patched device endpoint missing/ambiguous: " + row["manual_iec_address"] + " bits=" + _text(row["bit_size"]) + " matches=" + str(len(matches)))
        matches[0].io_mapping.variable = row["variable"]
    current = _inspect(project)
    verification = _verify_device_bindings(current, bindings, request["program"]["bindings"], request["application_name"])
    if not verification["passed"]:
        raise RuntimeError("Patched bindings failed inspection: " + _text(verification["diagnostics"]))
    _write_json(request["configured_inspection_path"], current)
    return {"passed": True, "parameters": patch["parameters"], "bindings_verification": verification,
            "inspection_path": request["configured_inspection_path"], "native_resize": native_resize}


def _application(project, name):
    candidates = [obj for obj, path in _walk(project) if _name(obj) == name and _flag(obj, "is_application")]
    if len(candidates) != 1:
        raise RuntimeError("Expected exactly one Application named " + name + "; found " + str(len(candidates)))
    # SP22 can return generic tree wrappers; active_application dispatches build.
    project.active_application = candidates[0]
    application = project.active_application
    if application is None or _name(application) != name:
        raise RuntimeError("Cannot activate selected Application")
    return application


def _update(application, program):
    name = program["program_name"]
    candidates = [obj for obj in application.find(name, True)
                  if _flag(obj, "has_textual_declaration") and _flag(obj, "has_textual_implementation")]
    if len(candidates) != 1:
        raise RuntimeError("Expected one existing textual program " + name + "; found " + str(len(candidates)))
    target = candidates[0]
    current = _document(target, "textual_declaration")
    if not isinstance(current, text_type) or not re.search(r"\bPROGRAM\s+" + re.escape(name) + r"\b", current, re.I):
        raise RuntimeError("Target is not the requested PROGRAM")
    # Preflight every global before editing anything; never overwrite a DUT/POU.
    globals_ = []
    for global_ in program["globals"]:
        matches = list(application.find(global_["name"], True))
        if len(matches) > 1:
            raise RuntimeError("Ambiguous existing global " + global_["name"])
        existing = matches[0] if matches else None
        if existing is not None:
            declaration = _document(existing, "textual_declaration")
            if not isinstance(declaration, text_type) or not re.search(r"\bVAR_GLOBAL\b", declaration, re.I):
                raise RuntimeError("Existing object is not a global variable list: " + global_["name"])
        globals_.append((global_, existing))
    target.textual_declaration.replace(new_text=program["declaration"])
    target.textual_implementation.replace(new_text=program["implementation"])
    for global_, existing in globals_:
        if existing is None:
            existing = application.create_gvl(global_["name"])
        existing.textual_declaration.replace(new_text=global_["declaration"])


def _severity(raw):
    # Severity is a flags value: FatalError=1, Error=2, Warning=4,
    # Information=8, Text=16 in the documented ScriptSystem API.
    try:
        value = int(raw)
        if value & 3: return "error"
        if value & 4: return "warning"
        if value & 8: return "info"
        if value & 16: return "status"
    except Exception:
        pass
    text = _text(raw).lower()
    if "error" in text or "fatal" in text: return "error"
    if "warning" in text: return "warning"
    if "information" in text: return "info"
    if text == "text": return "status"
    return "unknown"


def _messages(system):
    categories = list(system.get_message_categories())
    # Explicit Build category avoids the default ScriptMessage-only trap.
    if not any(_text(c).lower().strip("{}") == _BUILD_CATEGORY for c in categories):
        categories.append(_category_guid(_BUILD_CATEGORY))
    records, failures, build_queried = [], [], False
    for category in categories:
        try:
            messages = list(system.get_message_objects(_category_guid(category)))
            if _text(category).lower().strip("{}") == _BUILD_CATEGORY:
                build_queried = True
            for message in messages:
                # Text-only fallbacks cannot reliably certify compiler counts.
                severity = _severity(getattr(message, "severity", None))
                row = {"category": _text(category), "severity": severity,
                       "severity_raw": _text(getattr(message, "severity", None)),
                       "text": _text(getattr(message, "text", message)),
                       "position": _text(getattr(message, "position_text", "")),
                       "prefix": _text(getattr(message, "prefix", "")),
                       "number": _text(getattr(message, "number", ""))}
                records.append(row)
        except Exception as exc:
            failures.append({"code": "compiler_messages", "category": _text(category), "message": _text(exc)})
    verified = build_queried and not failures and not any(r["severity"] == "unknown" for r in records)
    return records, failures, verified


def _load_request():
    path = os.environ.get("FACTORYIO_CODESYS_REQUEST")
    if not path or not os.path.isabs(path):
        raise RuntimeError("Absolute FACTORYIO_CODESYS_REQUEST is required")
    with io.open(path, "r", encoding="utf-8-sig") as stream:
        request = json.load(stream)
    if request.get("schema_version") != 1 or not re.match(r"^[a-f0-9]{32}$", request.get("request_id", "")):
        raise RuntimeError("Invalid bridge request")
    if request.get("bridge_sha256") != _hash(globals()["__file__"]):
        raise RuntimeError("Bridge snapshot differs from request")
    directory = os.path.dirname(os.path.realpath(path))
    for field in ("result_path", "inspection_path", "configured_inspection_path"):
        if field not in request and field == "configured_inspection_path":
            continue
        target = request[field]
        if not os.path.isabs(target) or os.path.dirname(os.path.realpath(target)) != directory or os.path.lexists(target):
            raise RuntimeError("Artifact must be a new file in request directory: " + field)
    project = os.path.realpath(request["project_path"])
    if os.path.dirname(project) != os.path.join(directory, "project") or not project.lower().endswith(".project"):
        raise RuntimeError("Project must be a copied .project in request directory/project")
    if _hash(project) != request["source_sha256"]:
        raise RuntimeError("Copied baseline hash differs from request")
    return request


def _online_probe(project, application, spec, progress=None):
    """Inspect official API objects; Keep login never downloads or starts code."""
    return _online_session(project, application, spec, progress)


def _delay(milliseconds):
    system = globals().get("system")
    delay = getattr(system, "delay", None)
    if callable(delay):
        delay(milliseconds)  # Pump the IDE message loop, including online monitoring.
    else:
        time.sleep(milliseconds / 1000.0)


def _online_session(project, application, spec, progress=None, download=False):
    """Shared target/authentication path; download is called only after deployment gates."""
    result = {"status": "error", "connect_requested": spec.get("connect") is True,
              "connected": False, "logged_in": False, "download_requested": download,
              "start_called": False, "gateways": [], "capabilities": {}}
    def checkpoint(stage):
        result["stage"] = stage
        if progress is not None:
            progress(result)

    def sanitized(value):
        text = _text(value)
        for name in (spec.get("credential_env") or {}).values():
            secret = os.environ.get(name)
            if secret:
                text = text.replace(secret, "[redacted]")
        return text

    credentials_applied = False
    try:
        checkpoint("inspect")
        expressions = spec.get("expressions")
        if expressions is not None and (not isinstance(expressions, list) or len(expressions) > 50 or not all(
                isinstance(e, (str, text_type)) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$", e) for e in expressions)):
            raise RuntimeError("Monitoring accepts only up to 50 qualified variable names")
        online_api = globals()["online"]
        options = globals()["OnlineChangeOption"]
        result["capabilities"]["keep_login"] = hasattr(options, "Keep")
        result["capabilities"]["create_online_application"] = callable(getattr(online_api, "create_online_application", None))
        for gateway in online_api.gateways:
            params = gateway.config_params
            result["gateways"].append({"name": _text(gateway.name), "guid": _text(gateway.guid),
                "driver": _text(gateway.gateway_driver.name),
                "config_params": dict((_text(k), _text(v)) for k, v in params.items())})
        devices = [(obj, path) for obj, path in _walk(project)
                   if _flag(obj, "is_device") and _name(obj) == spec.get("device_name", "Device")]
        if len(devices) != 1:
            raise RuntimeError("Expected exactly one target device for online probe")
        device, device_path = devices[0]
        simulation = device.get_simulation_mode()
        result["simulation_mode_before"] = _text(simulation)
        if _text(simulation).lower() not in ("true", "false"):
            raise RuntimeError("Cannot verify target device simulation mode")
        settings = device.get_device_communication_settings()
        result["configured_target"] = {"device_path": device_path,
            "settings": _properties(settings, ("gateway_guid", "device_address", "device_name", "TrackingMode"))}
        result["capabilities"]["set_gateway_and_address"] = callable(getattr(device, "set_gateway_and_address", None))
        if spec.get("connect") is True:
            if not result["capabilities"]["keep_login"]:
                raise RuntimeError("Keep login is unavailable; probe will not fall back to download")
            host, port = spec.get("host", "127.0.0.1"), spec.get("port", 11740)
            if host != "127.0.0.1" or type(port) is not int or not 1 <= port <= 65535:
                raise RuntimeError("This probe supports only explicit loopback targets")
            local_gateways = [g for g in online_api.gateways if
                any(_text(v).lower() in ("127.0.0.1", "localhost", "::1") for v in g.config_params.values())]
            if spec.get("gateway_name"):
                local_gateways = [g for g in local_gateways if _text(g.name) == spec["gateway_name"]]
            if len(local_gateways) != 1:
                raise RuntimeError("Expected one gateway with a verified loopback address")
            gateway = local_gateways[0]
            result["attempted_target"] = {"host": host, "port": port, "gateway_guid": _text(gateway.guid)}
            sources = globals().get("CredentialSourceKind")
            mode = getattr(sources, "CurrentProjectSession", None)
            if mode is None:
                raise RuntimeError("Noninteractive authentication mode is unavailable; probe will not prompt")
            online_api.auth_fallback_modes = mode
            result["authentication_mode"] = "CurrentProjectSession; interactive login disabled"
            credential_env = spec.get("credential_env")
            if credential_env is not None:
                if (not isinstance(credential_env, dict) or set(credential_env) != set(("username_env", "password_env")) or
                        not all(isinstance(name, (str, text_type)) and re.match(r"^[A-Z][A-Z0-9_]{0,63}$", name)
                                for name in credential_env.values())):
                    raise RuntimeError("Invalid credential environment references")
                username = os.environ.get(credential_env["username_env"])
                password = os.environ.get(credential_env["password_env"])
                if not username or password is None:
                    raise RuntimeError("Configured PLC credential environment variables are missing")
            checkpoint("scan")
            address = gateway.find_address_by_ip(host, port)
            if not address:
                raise RuntimeError("Loopback scan returned no CODESYS device address")
            result["attempted_target"]["device_address"] = _text(address)
            # IDE simulation belongs to this IDE process and can report Run
            # while the external Control Win Runtime remains stopped.
            if simulation:
                device.set_simulation_mode(False)
            result["simulation_mode_after"] = _text(device.get_simulation_mode())
            if result["simulation_mode_after"].lower() != "false":
                raise RuntimeError("Target still uses IDE simulation; external Runtime is unverified")
            # Only the owned project copy in this process is modified; no save.
            device.set_gateway_and_address(gateway, address)
            if credential_env is not None:
                # Scope credentials to the resolved current target, not the saved old gateway.
                online_api.set_specific_credentials(device, username, password)
                credentials_applied = True
                result["credentials_supplied"] = True
            with online_api.create_online_application(application) as online_app:
                for method in ("login", "logout", "start", "stop", "reset", "read_value", "read_values"):
                    result["capabilities"][method] = callable(getattr(online_app, method, None))
                result["online_timeout_default"] = getattr(online_app, "timeout", None)
                online_app.timeout = 15
                result["online_timeout_seconds"] = 15
                checkpoint("login")
                online_app.login(options.Never if download else options.Keep, False)
                result["logged_in"] = online_app.is_logged_in is True
                if not result["logged_in"]:
                    raise RuntimeError("Keep login did not complete; no download was attempted")
                result["connected"] = True
                result['application_state_before'] = _text(online_app.application_state)
                action = spec.get('action')
                if action not in (None, 'stop', 'start', 'reset') or (download and action is not None):
                    raise RuntimeError('Invalid lifecycle operation; cannot combine it with download')
                if action == 'reset' and spec.get('reset_mode') not in ('warm','cold'):
                    raise RuntimeError('Only warm and cold resets are supported')
                if action in ('stop', 'reset'):
                    checkpoint('stop')
                    if _text(online_app.application_state).lower() != 'stop':
                        online_app.stop()
                        result['stop_called'] = True
                    else:
                        result['stop_already_satisfied'] = True
                    deadline = time.time() + 5
                    while _text(online_app.application_state).lower() != 'stop' and time.time() < deadline:
                        _delay(100)
                    if _text(online_app.application_state).lower() != 'stop':
                        raise RuntimeError('PLC Stop was not observed')
                    result['stopped_before_reset'] = True
                    if action == 'reset':
                        mode = spec.get('reset_mode')
                        if mode not in ('warm', 'cold'):
                            raise RuntimeError('Only warm and cold resets are supported')
                        checkpoint('reset_' + mode)
                        reset_options = globals()['ResetOption']
                        online_app.reset(getattr(reset_options, 'Warm' if mode == 'warm' else 'Cold'), False)
                        result['reset_called'] = True
                        result['reset_mode'] = mode
                        _delay(500)
                        if _text(online_app.application_state).lower() != 'stop':
                            raise RuntimeError('PLC reset did not leave the application stopped')
                elif action == 'start':
                    checkpoint('start_existing')
                    if _text(online_app.application_state).lower() != 'run':
                        online_app.start()
                        result['start_called'] = True
                    else:
                        result['start_already_satisfied'] = True
                    deadline = time.time() + 5
                    while _text(online_app.application_state).lower() != 'run' and time.time() < deadline:
                        _delay(100)
                    if _text(online_app.application_state).lower() != 'run':
                        raise RuntimeError('PLC Start was not observed')
                result['lifecycle_action'] = action
                if download:
                    checkpoint("start")
                    online_app.start()
                    result["start_called"] = True
                    deadline = time.time() + 5
                    while _text(online_app.application_state).lower() != "run" and time.time() < deadline:
                        _delay(100)
                _delay(500)
                result["application_state"] = _text(online_app.application_state)
                result["operation_state"] = _text(online_app.operation_state)
                if expressions:
                    try:
                        for e in expressions:
                            online_app.read_value(e)  # Register subscriptions before waiting.
                        _delay(1000)
                        result["values"] = dict((e, _text(online_app.read_value(e))) for e in expressions)
                        result["application_state"] = _text(online_app.application_state)
                        result["operation_state"] = _text(online_app.operation_state)
                    except Exception as exc:
                        result["monitor_error"] = sanitized(exc)
                checkpoint("logout")
                online_app.logout()
        result["status"] = "ok"
        checkpoint("complete")
    except Exception as exc:
        result["error_stage"] = result.get("stage")
        result["error"] = sanitized(exc)
        result["traceback"] = sanitized(traceback.format_exc())
        checkpoint("failed")
    finally:
        if credentials_applied:
            try:
                online_api.clear_all_credentials()
                result["script_credentials_cleared"] = True
            except Exception:
                result["script_credentials_cleared"] = False
    return result


def main():
    request = _load_request()
    result = {"schema_version": 1, "request_id": request["request_id"], "compiled": False, "started": False,
              "bindings_verified": False, "task_program_verified": False,
              "build_completed": False, "messages_verified": False, "messages": [], "error_count": 0,
              "warning_count": 0, "diagnostics": [], "project_path": request["project_path"],
              "source_sha256": request["source_sha256"], "project_sha256": None}
    project = None
    try:
        system = globals()["system"]
        if system.ui_present:
            raise RuntimeError("This bridge requires its own --noUI CODESYS process")
        project = globals()["projects"].open(request["project_path"])
        inspection = _inspect(project)
        _write_json(request["inspection_path"], inspection)
        result["inspection_path"] = request["inspection_path"]
        result["diagnostics"].extend(inspection["diagnostics"])
        application = _application(project, request["application_name"])
        initial_bindings, initial_task = None, None
        if request.get("program") is not None:
            initial_bindings = _verify_device_bindings(inspection, request.get("verified_device_bindings"), request["program"]["bindings"], request["application_name"])
            initial_task = _verify_task_program(inspection, request["application_name"], request["expected_task_program"])
            result["bindings_verification"] = initial_bindings
            result["task_verification"] = initial_task
            _update(application, request["program"])
            result["device_preparation"] = _apply_device_patch(project, request)
        # Own headless process; clear only its Build category, never all categories.
        system.clear_messages(_category_guid(_BUILD_CATEGORY))
        application.build()
        result["build_completed"] = True
        records, failures, verified = _messages(system)
        result["messages"], result["messages_verified"] = records, verified
        result["diagnostics"].extend(failures)
        result["error_count"] = sum(1 for r in records if r["severity"] == "error")
        result["warning_count"] = sum(1 for r in records if r["severity"] == "warning")
        project.save()
        result["project_sha256"] = _hash(request["project_path"])
        result["compiled"] = verified and result["error_count"] == 0
        if request.get("online_probe") is not None:
            if request.get("deploy"):
                raise RuntimeError("Read-only online probe cannot be combined with deployment")
            def save_probe_progress(probe):
                path = os.path.join(os.path.dirname(request["inspection_path"]),
                                    "online-probe-" + probe["stage"] + ".json")
                _write_json(path, {"request_id": request["request_id"], "compiled": result["compiled"],
                                  "online_probe": probe})
            result["online_probe"] = _online_probe(project, application, request["online_probe"], save_probe_progress)
        if request.get("program") is not None and not request.get("deploy"):
            current = _inspect(project)
            current_bindings = _verify_device_bindings(current, request.get("verified_device_bindings"), request["program"]["bindings"], request["application_name"])
            current_task = _verify_task_program(current, request["application_name"], request["expected_task_program"])
            result["bindings_verification_after_build"] = current_bindings
            result["task_verification_after_build"] = current_task
            result["bindings_verified"] = current_bindings["passed"]
            result["task_program_verified"] = current_task["passed"]
            _write_json(os.path.join(os.path.dirname(request["result_path"]), "post-build-inspection.json"), current)
        if request.get("deploy"):
            if not result["compiled"] or not request.get("deployment_ready") or not request.get("transport_confirmed"):
                raise RuntimeError("Deployment preconditions were not satisfied")
            if request.get("verified_template_sha256") != request["source_sha256"]:
                raise RuntimeError("Verified device evidence is not tied to this baseline source SHA")
            if request["expected_task_program"].lower() != request["program"]["program_name"].lower():
                raise RuntimeError("Expected task program differs from the generated program")
            # Re-read actual channel mappings and task calls immediately before login.
            current = _inspect(project)
            current_bindings = _verify_device_bindings(current, request.get("verified_device_bindings"), request["program"]["bindings"], request["application_name"])
            current_task = _verify_task_program(current, request["application_name"], request["expected_task_program"])
            result["bindings_verification_before_deploy"] = current_bindings
            result["task_verification_before_deploy"] = current_task
            result["bindings_verified"] = bool(initial_bindings and initial_bindings["passed"] and current_bindings["passed"])
            result["task_program_verified"] = bool(initial_task and initial_task["passed"] and current_task["passed"])
            if not result["bindings_verified"] or not result["task_program_verified"]:
                raise RuntimeError("Actual device mappings or expected task program are unverified; deployment blocked")
            # Official full-download option is Never, not an undocumented Download.
            result["download_option"] = "OnlineChangeOption.Never"
            spec = request.get("deployment_online")
            if not isinstance(spec, dict) or spec.get("connect") is not True:
                raise RuntimeError("Deployment requires an explicit loopback online target")
            result["deployment_online"] = _online_session(project, application, spec, download=True)
            session = result["deployment_online"]
            result["application_state"] = session.get("application_state")
            result["operation_state"] = session.get("operation_state")
            result["started"] = session.get("status") == "ok" and session.get("start_called") is True and (session.get("application_state") or "").lower() == "run"
            if not result["started"]:
                raise RuntimeError("Deployment/start failed: " + session.get("error", "PLC did not enter Run"))
    except Exception as exc:
        result["diagnostics"].append({"code": "bridge_exception", "message": _text(exc), "traceback": traceback.format_exc()})
    finally:
        if project is not None:
            try:
                project.close()
            except Exception as exc:
                result["diagnostics"].append({"code": "project_close", "message": _text(exc)})
        try:
            result["project_sha256"] = _hash(request["project_path"])
        except Exception as exc:
            result["compiled"] = result["started"] = False
            result["diagnostics"].append({"code": "project_hash", "message": _text(exc)})
        _write_json(request["result_path"], result)


if __name__ == "__main__":
    main()
