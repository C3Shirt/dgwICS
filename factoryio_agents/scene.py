"""Conservative Factory I/O XML composition from verified saved object templates.

This module checks the observed XML format, not Factory I/O's internal prefab
registry or runtime behavior. All coordinates use the file's native grid units.
No source file is modified. Only existing, explicitly allowed settings are edited.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import uuid
import xml.etree.ElementTree as ET


IO_TYPES = frozenset({"BinaryInput", "BinaryOutput", "FloatInput", "FloatOutput",
                      "AnalogueInput", "AnalogueOutput", "IntInput", "IntOutput"})
CHANNEL = re.compile(r"^(Bit|Float|Int|Numeric)(Input|Output)(0|[1-9][0-9]*)$")
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
_PATH_PART = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\[[1-9][0-9]*\])?$")
_SYSTEM_TYPES = {
    "RunInputKey": "BinaryInput", "PauseInputKey": "BinaryInput",
    "ResetInputKey": "BinaryInput", "TimeScaleInputKey": "AnalogueInput",
    "RunOutputKey": "BinaryOutput", "PauseOutputKey": "BinaryOutput",
    "ResetOutputKey": "BinaryOutput", "CameraIndexOutputKey": "IntOutput",
}
_SAFE_ATTRIBUTES = {
    "Sensor": {"Range"}, "Encoder": {"Enabled"},
    "Emitter": {"MinTimeToEmit", "MaxTimeToEmit", "UpTo",
                "RandomPartPosition", "RandomPartOrientation"},
    "Bases": {"Pallet", "SquarePallet", "StackableBox"},
    "Parts": {"BoxL", "BoxM", "BoxS", "PalletizingBox", "ProductBaseBlue",
              "ProductBaseGreen", "ProductBaseMetal", "ProductLidBlue",
              "ProductLidGreen", "ProductLidMetal", "RawMaterialBlue",
              "RawMaterialGreen", "RawMaterialMetal", "StackableBox"},
}
_IO_ATTRIBUTES = {"Name", "Address", "OpenCircuit", "ShortCircuit",
                  "UseForcedValue", "ForcedValue"}
_XML_INTEGER = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_XML_REAL = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_INTEGER_MAX = 2**31 - 1  # Conservative scalar representation limit, not an enum table.
_INTEGER_MIN = -(2**31)
MODBUS_CLIENT_CHANNEL_LIMITS = {"Bit": 256, "Numeric": 64, "Float": 64, "Int": 64}
MODBUS_IMPLEMENTATION_MAX_ADDRESS = 65535
_MODBUS_LIMIT_SOURCE = "https://docs.factoryio.com/manual/drivers/modbus-client/"


def _limit_summary() -> dict:
    return {"Bit_channel_count": 256, "Numeric_channel_count": 64,
            "documented_count_source": _MODBUS_LIMIT_SOURCE,
            "Float_and_Int_channel_count": 64,
            "Float_and_Int_limit_basis": "conservative_implementation_policy",
            "offset_and_slot_address_max": MODBUS_IMPLEMENTATION_MAX_ADDRESS,
            "address_limit_basis": "implementation_policy",
            "scalar_integer_max": _INTEGER_MAX,
            "scalar_integer_limit_basis": "implementation_policy",
            "register_width_confirmed": False}


class SceneError(ValueError):
    """An unsafe plan, stale catalog, or invalid observed XML scene."""


def _xml_integer(value: str | None, minimum: int = 0, maximum: int = _INTEGER_MAX) -> int:
    if value is None or not _XML_INTEGER.fullmatch(value):
        raise SceneError("Expected an integer in decimal syntax")
    try:
        integer = int(value)
    except ValueError as exc:
        raise SceneError("Integer exceeds the implementation representation limit") from exc
    if not minimum <= integer <= maximum:
        raise SceneError(f"Integer must be between {minimum} and {maximum} (implementation range)")
    return integer


def _xml_real(value: str | None, minimum: float | None = None) -> float:
    if value is None or not _XML_REAL.fullmatch(value):
        raise SceneError("Expected a finite number in decimal/exponent syntax")
    real = float(value)
    if not math.isfinite(real) or (minimum is not None and real < minimum):
        raise SceneError("Expected a finite number" + (f" >= {minimum}" if minimum is not None else ""))
    return real


def _scalar_issues(root: ET.Element) -> list[dict]:
    """Check known scalar types; operating mode numbers are not interpreted."""
    issues = []
    for node in root.iter():
        checks = {}
        if node.tag in IO_TYPES:
            checks["Address"] = lambda v: _xml_integer(v)
            for attribute in ("OpenCircuit", "ShortCircuit", "UseForcedValue"):
                checks[attribute] = "bool"
            checks["ForcedValue"] = ("bool" if _kind(node.tag) == "bool" else (
                (lambda v: _xml_integer(v, _INTEGER_MIN)) if _kind(node.tag) == "int" else _xml_real))
        elif node.tag == "ComponentIO":
            checks = {"Index": lambda v: _xml_integer(v, -1),
                      "CurrentOperatingMode": lambda v: _xml_integer(v)}
            if "Color" in node.attrib:
                checks["Color"] = lambda v: _xml_integer(v)
        elif node.tag == "Object" and "Locked" in node.attrib:
            checks["Locked"] = "bool"
        elif node.tag == "Sensor":
            checks["Range"] = lambda v: _xml_real(v, 0)
        elif node.tag == "Encoder":
            checks["Enabled"] = "bool"
        elif node.tag == "Emitter":
            checks = {"MinTimeToEmit": lambda v: _xml_real(v, 0),
                      "MaxTimeToEmit": lambda v: _xml_real(v, 0),
                      "UpTo": lambda v: _xml_integer(v),
                      "RandomPartPosition": "bool", "RandomPartOrientation": "bool"}
        elif node.tag in {"Bases", "Parts"}:
            checks = {attribute: "bool" for attribute in node.attrib}
        elif node.tag == "Instructor":
            checks = {attribute: "bool" for attribute in
                      ("LockCurrentParts", "RunModeOnly", "HideFailures") if attribute in node.attrib}
        elif node.tag == "Drivers" and "CurrentDriver" in node.attrib:
            checks["CurrentDriver"] = lambda v: _xml_integer(v)
        for attribute, check in checks.items():
            try:
                value = node.get(attribute)
                if check == "bool":
                    if value not in {"True", "False"}:
                        raise SceneError("Expected a saved XML Boolean True or False")
                else:
                    check(value)
            except SceneError as exc:
                issues.append({"code": "scalar", "element": node.tag, "attribute": attribute,
                               "message": str(exc)})
        if node.tag in {"FinePosition", "FineRotation"}:
            try:
                _xml_real((node.text or "").strip(), 0 if node.tag == "FinePosition" else None)
            except SceneError as exc:
                issues.append({"code": "scalar", "element": node.tag, "message": str(exc)})
        if node.tag == "Emitter":
            try:
                if _xml_real(node.get("MinTimeToEmit"), 0) > _xml_real(node.get("MaxTimeToEmit"), 0):
                    issues.append({"code": "scalar_range", "element": "Emitter",
                                   "message": "MinTimeToEmit cannot exceed MaxTimeToEmit"})
            except SceneError:
                pass  # Field-specific errors already recorded above.
    return issues


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load(path: Path) -> tuple[bytes, ET.Element]:
    data = Path(path).read_bytes()
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise SceneError("DTD and entity declarations are not supported")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise SceneError(f"Invalid scene XML: {path}: {exc}") from exc
    if root.tag != "FactoryIO":
        raise SceneError(f"Expected FactoryIO XML root, got {root.tag!r}: {path}")
    return data, root


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SceneError(f"{label} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise SceneError(f"{label} must be finite") from exc
    if not math.isfinite(number):
        raise SceneError(f"{label} must be finite")
    return number


def _vector(value: Any, length: int, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise SceneError(f"{label} must have {length} numbers")
    return [_number(v, label) for v in value]


def _xml_vector(node: ET.Element | None, axes: str) -> list[float]:
    if node is None:
        raise SceneError("Missing transform element")
    try:
        values = [_xml_real(node.attrib[a]) for a in axes]
    except (KeyError, ValueError) as exc:
        raise SceneError(f"Invalid {node.tag} vector") from exc
    if not all(math.isfinite(v) for v in values):
        raise SceneError(f"Nonfinite {node.tag} vector")
    return values


def _fmt(value: float) -> str:
    return format(0.0 if abs(value) < 1e-14 else value, ".15g")


def _kind(io_type: str) -> str:
    return "bool" if io_type.startswith("Binary") else (
        "int" if io_type.startswith("Int") else "float")


def _direction(io_type: str) -> str:
    return "Input" if io_type.endswith("Input") else "Output"


def _tag_nodes(obj: ET.Element) -> list[ET.Element]:
    return [node for node in obj.iter() if node.tag in IO_TYPES]


def _isolate_native_addresses(root: ET.Element) -> list[dict]:
    # Native BitIO/FloatIO/IntIO store names and values in MemoryMap[type,direction,address].
    # PointIOKey identifies the driver mapping, but distinct Keys at one address share native memory.
    banks = defaultdict(list)
    for obj in root.findall('Object'):
        for node in _tag_nodes(obj):
            banks[_kind(node.tag), _direction(node.tag)].append(node)
    changes = []
    for bank, nodes in banks.items():
        used = {int(node.get('Address')) for node in nodes}
        seen = set()
        # Stable GUID ordering makes the allocation independent of object placement/order.
        for node in sorted(nodes, key=lambda node: node.get('Key')):
            old = int(node.get('Address'))
            if old not in seen:
                seen.add(old); continue
            address = next(value for value in range(len(used) + 1) if value not in used)
            # Observed native system banks start at Bit 496 / Float and Int 240 in 2.5.8.
            if address >= (496 if bank[0] == 'bool' else 240):
                raise SceneError('No free native I/O address below the system memory bank')
            used.add(address); seen.add(address); node.set('Address', str(address))
            changes.append({'tag_key': node.get('Key'), 'data_type': bank[0], 'direction': bank[1],
                            'old_address': old, 'new_address': address})
    return changes


def _tag(node: ET.Element, index: int) -> dict:
    return {"index": index, "key": node.get("Key"), "name": node.get("Name", ""),
            "type": node.tag, "kind": _direction(node.tag).lower(), "data_type": _kind(node.tag),
            "direction": _direction(node.tag), "address": node.get("Address"),
            "forced": {"enabled": node.get("UseForcedValue") == "True",
                       "value": node.get("ForcedValue")},
            "use_forced_value": node.get("UseForcedValue") == "True",
            "forced_value": node.get("ForcedValue"),
            "open_circuit": node.get("OpenCircuit") == "True",
            "short_circuit": node.get("ShortCircuit") == "True"}


def _system_tags(drivers: ET.Element | None) -> list[dict]:
    if drivers is None:
        return []
    return [{"id": "system/" + field.removesuffix("Key"), "key": drivers.get(field),
             "name": field.removesuffix("Key"), "type": io_type,
             "kind": _direction(io_type).lower(), "data_type": _kind(io_type), "direction": _direction(io_type),
             "address": None, "index": None, "object_id": None,
             "system_field": field}
            for field, io_type in _SYSTEM_TYPES.items() if drivers.get(field)]


def _compact(node: ET.Element) -> dict:
    def bounded(value: str | None) -> str | dict | None:
        if value is not None and len(value) > 200:
            return {"characters": len(value), "sha256_utf8": _sha(value.encode("utf-8"))}
        return value
    return {"element": node.tag,
            "attributes": {k: bounded(v) for k, v in node.attrib.items()
                           if k != "Data" or node.tag != "Screenshot"},
            "text": bounded((node.text or "").strip() or None),
            "children": [_compact(child) for child in node if child.tag != "Screenshot"]}


def _basis(q: list[float]) -> dict[str, list[float]]:
    x, y, z, w = q
    return {"Right": [1 - 2 * (y*y + z*z), 2 * (x*y + w*z), 2 * (x*z - w*y)],
            "Up": [2 * (x*y - w*z), 1 - 2 * (x*x + z*z), 2 * (y*z + w*x)],
            "Forward": [2 * (x*z + w*y), 2 * (y*z - w*x), 1 - 2 * (x*x + y*y)]}


def _compatible(channel: str, tag: dict) -> bool:
    match = CHANNEL.fullmatch(channel)
    if not match:
        return False
    family, direction, _ = match.groups()
    allowed = {"Bit": {"bool"}, "Float": {"float"}, "Int": {"int"},
               "Numeric": {"int", "float"}}
    return tag["direction"] == direction and tag["data_type"] in allowed[family]


def _validate_root(root: ET.Element) -> dict:
    issues: list[dict] = _scalar_issues(root)
    def issue(code: str, message: str, **details: Any) -> None:
        issues.append({"code": code, "message": message, **details})
    if root.tag != "FactoryIO":
        issue("root", "Expected FactoryIO root")
    objects = root.findall("Object")
    object_keys, tag_keys = [], []
    targets = defaultdict(list)
    for ordinal, obj in enumerate(objects, 1):
        proxies = obj.findall("Proxy")
        if len(proxies) != 1 or not proxies[0].get("Key") or not proxies[0].get("PrefabName"):
            issue("proxy", "Each object needs one keyed prefab Proxy", ordinal=ordinal)
            continue
        proxy = proxies[0]
        object_keys.append(proxy.get("Key"))
        try:
            _xml_vector(proxy.find("Position"), "XYZ")
            for name in ('Position', 'Forward', 'Up', 'Right'):
                node = proxy.find(name)
                if node is not None and any(not re.fullmatch(r'[+-]?\d+', node.get(a, '')) for a in 'XYZ'):
                    issue('native_grid_integer', 'Native voxel vectors must contain integer XML scalars', ordinal=ordinal, vector=name)
            q = _xml_vector(proxy.find("Rotation"), "XYZW")
            if abs(sum(v*v for v in q) - 1) > 1e-4:
                issue("quaternion", "Quaternion must be normalized", ordinal=ordinal)
            for name, expected in _basis(q).items():
                actual = _xml_vector(proxy.find(name), "XYZ")
                if max(abs(a-b) for a, b in zip(actual, expected)) > 1e-4:
                    issue("basis", "Quaternion and direction vectors disagree", ordinal=ordinal,
                          vector=name)
        except SceneError as exc:
            issue("transform", str(exc), ordinal=ordinal)
        for index, node in enumerate(_tag_nodes(obj)):
            if not node.get("Key"):
                issue("tag_key", "Every I/O tag needs a Key", ordinal=ordinal, index=index)
            else:
                tag_keys.append(node.get("Key"))
                targets[node.get("Key")].append(_tag(node, index))
    drivers_nodes = root.findall("Drivers")
    if len(drivers_nodes) > 1:
        issue("drivers", "Only one Drivers block is supported")
    drivers = root.find("Drivers")
    systems = _system_tags(drivers)
    for tag in systems:
        targets[tag["key"]].append(tag)
    for label, values in (("object", object_keys), ("io", tag_keys),
                          ("all_identity", object_keys + tag_keys + [s["key"] for s in systems])):
        for key, count in Counter(values).items():
            if count > 1:
                issue("duplicate_" + label + "_key", "Identity Key appears multiple times",
                      key=key, count=count)
    if drivers is not None:
        for name, count in Counter(driver.tag for driver in drivers).items():
            if count > 1:
                issue("duplicate_driver", "Driver block appears multiple times", driver=name)
        for driver in drivers:
            channels, mapped_keys = [], []
            counts, offsets = driver.find("PointIOCount"), driver.find("PointIOOffset")
            for name in ("PointIOCount", "PointIOOffset", "Properties"):
                if len(driver.findall(name)) > 1:
                    issue("duplicate_driver_configuration", "Driver configuration node must be a singleton",
                          driver=driver.tag, element=name)
            for name in ("PointIOCount", "PointIOOffset"):
                for configuration in driver.findall(name):
                    for field, value in configuration.attrib.items():
                        try:
                            maximum = _INTEGER_MAX
                            if driver.tag == "ModbusTCPClient":
                                maximum = MODBUS_IMPLEMENTATION_MAX_ADDRESS if name == "PointIOOffset" else maximum
                                field_match = re.fullmatch(r"(Bit|Float|Int|Numeric)(Input|Output)Count", field)
                                if name == "PointIOCount" and field_match:
                                    maximum = MODBUS_CLIENT_CHANNEL_LIMITS[field_match.group(1)]
                            _xml_integer(value, maximum=maximum)
                        except SceneError as exc:
                            issue("channel_configuration", str(exc), driver=driver.tag, field=field)
            if driver.tag == "ModbusTCPClient":
                for configuration in driver.findall("Properties"):
                    for field in ("ReadCoils", "ReadHoldingRegisters"):
                        if field in configuration.attrib and configuration.get(field) not in {"True", "False"}:
                            issue("scalar", "Expected a saved XML Boolean True or False",
                                  driver=driver.tag, element="Properties", attribute=field)
                    if "ScaleFactor" in configuration.attrib:
                        try:
                            if _xml_real(configuration.get("ScaleFactor"), 0) == 0:
                                raise SceneError("ScaleFactor must be positive")
                        except SceneError as exc:
                            issue("scalar", str(exc), driver=driver.tag,
                                  element="Properties", attribute="ScaleFactor")
                for family, limit in MODBUS_CLIENT_CHANNEL_LIMITS.items():
                    for direction in ("Input", "Output"):
                        try:
                            count = _xml_integer(counts.get(family + direction + "Count"), maximum=limit) if counts is not None else 0
                            offset = _xml_integer(offsets.get(family + direction + "Offset"),
                                                  maximum=MODBUS_IMPLEMENTATION_MAX_ADDRESS) if offsets is not None else 0
                            if count and offset + count - 1 > MODBUS_IMPLEMENTATION_MAX_ADDRESS:
                                issue("modbus_address_limit", "Configured offset plus slot span exceeds implementation address limit",
                                      driver=driver.tag, family=family, direction=direction)
                        except SceneError:
                            pass  # Reported in configuration checks, or missing fields below.
            for element in driver:
                if "PointIOKey" not in element.attrib:
                    continue
                channels.append(element.tag)
                key = element.get("PointIOKey")
                mapped_keys.append(key)
                target = targets.get(key, [])
                if len(target) != 1:
                    issue("driver_reference", "Driver reference is unresolved or ambiguous",
                          driver=driver.tag, channel=element.tag, key=key)
                match = CHANNEL.fullmatch(element.tag)
                if not match:
                    issue("channel", "Unsupported driver channel name", driver=driver.tag,
                          channel=element.tag)
                    continue
                family, direction, number = match.groups()
                if len(target) == 1 and not _compatible(element.tag, target[0]):
                    issue("mapping_type", "Channel family/direction does not match the I/O tag",
                          driver=driver.tag, channel=element.tag, type=target[0]["type"])
                try:
                    count = _xml_integer(counts.get(family + direction + "Count")) if counts is not None else 0
                    offset = _xml_integer(offsets.get(family + direction + "Offset")) if offsets is not None else 0
                    index = _xml_integer(number)
                    if count <= index:
                        raise SceneError("Channel exceeds configured count")
                    if driver.tag == "ModbusTCPClient" and (
                            index >= MODBUS_CLIENT_CHANNEL_LIMITS[family] or
                            offset + index > MODBUS_IMPLEMENTATION_MAX_ADDRESS):
                        raise SceneError("Modbus channel or offset plus channel exceeds supported implementation bounds")
                except SceneError:
                    issue("channel_bounds", "Channel exceeds configured count or has invalid offset",
                          driver=driver.tag, channel=element.tag)
            for label, values in (("channel", channels), ("target", mapped_keys)):
                for key, count in Counter(values).items():
                    if count > 1:
                        issue("mapping_collision", "Driver has duplicate " + label,
                              driver=driver.tag, value=key)
    return {"passed": not issues, "issues": issues, "scope": "observed_xml_structure",
            "supported_limits": _limit_summary(),
            "software_load_confirmed": False, "transport_confirmed": False,
            "object_count": len(objects), "object_tag_count": len(tag_keys)}


def validate_scene(path: Path) -> dict:
    """Report structural errors; success does not prove a Factory I/O load or run."""
    try:
        data, root = _load(Path(path))
        return {**_validate_root(root), "scene_path": str(Path(path).resolve()),
                "scene_sha256": _sha(data)}
    except (OSError, SceneError) as exc:
        return {"passed": False, "issues": [{"code": "load", "message": str(exc)}],
                "scope": "observed_xml_structure", "software_load_confirmed": False,
                "transport_confirmed": False}


def build_catalog(sources: list[Path]) -> dict:
    """Create a JSON-safe template catalog without embedding XML or screenshots."""
    result = {"schema_version": 1, "sources": [], "templates": []}
    ids, paths = set(), set()
    for source in sources:
        path = Path(source).resolve()
        if os.path.normcase(str(path)) in paths:
            continue
        paths.add(os.path.normcase(str(path)))
        data, root = _load(path)
        validation = _validate_root(root)
        if not validation["passed"]:
            raise SceneError(f"Invalid catalog source {path}: {validation['issues']}")
        digest = _sha(data)
        source_id = "source-" + digest[:20]
        if source_id in ids:
            # Identical content is one source; its verified path is sufficient.
            previous = next(s for s in result["sources"] if s["id"] == source_id)
            if previous["sha256"] != digest:
                raise SceneError("Catalog source hash-prefix collision")
            continue
        ids.add(source_id)
        objects = root.findall("Object")
        drivers = root.find("Drivers")
        result["sources"].append({"id": source_id, "path": str(path), "sha256": digest,
                                  "byte_count": len(data), "object_count": len(objects),
                                  "root_attributes": dict(root.attrib),
                                  "driver_names": [d.tag for d in drivers] if drivers is not None else [],
                                  "system_tags": _system_tags(drivers)})
        for ordinal, obj in enumerate(objects, 1):
            proxy = obj.find("Proxy")
            result["templates"].append({
                "template_id": f"{source_id}/object/{ordinal}", "source_id": source_id,
                "source_path": str(path), "source_sha256": digest, "ordinal": ordinal,
                "prefab": proxy.get("PrefabName"), "source_object_key": proxy.get("Key"),
                "position": _xml_vector(proxy.find("Position"), "XYZ"),
                "rotation": _xml_vector(proxy.find("Rotation"), "XYZW"),
                "tags": [_tag(node, index) for index, node in enumerate(_tag_nodes(obj))],
                "configuration": {"object_attributes": dict(obj.attrib),
                                  "nodes": [_compact(c) for c in obj if c.tag != "Proxy"]}})
    return result


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "/" in value or any(ord(c) < 32 for c in value):
        raise SceneError(f"{label} must be a nonempty stable identifier without slashes/control characters")
    return value


def _setting_descriptor(setting: dict) -> tuple[str, str, Any]:
    if not isinstance(setting, dict) or set(setting) - {"path", "attribute", "value"}:
        raise SceneError("Settings require only path, attribute, and value")
    path, attribute = setting.get("path"), setting.get("attribute")
    if not isinstance(path, str) or not path or not all(_PATH_PART.fullmatch(p) for p in path.split("/")):
        raise SceneError("Settings path must be a relative existing element path with optional 1-based indices")
    element_name = path.rsplit("/", 1)[-1].split("[")[0]
    allowed = _IO_ATTRIBUTES if element_name in IO_TYPES else _SAFE_ATTRIBUTES.get(element_name, set())
    if attribute not in allowed:
        raise SceneError(f"Unsafe or unsupported setting: {path}/@{attribute}")
    if "value" not in setting or not isinstance(setting["value"], (str, bool, int, float)):
        raise SceneError("Settings value must be a scalar string, bool, or finite number")
    if isinstance(setting["value"], float) and not math.isfinite(setting["value"]):
        raise SceneError("Settings value must be finite")
    return path, attribute, setting["value"]


def _boolean(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return "True" if value.lower() == "true" else "False"
    raise SceneError("Boolean setting requires true or false")


def _apply_settings(obj: ET.Element, settings: Any) -> None:
    if not isinstance(settings, list):
        raise SceneError("settings must be a list")
    for setting in settings:
        path, attribute, value = _setting_descriptor(setting)
        nodes = obj.findall(path)
        if len(nodes) != 1 or attribute not in nodes[0].attrib:
            raise SceneError(f"Setting must resolve to one existing node and attribute: {path}/@{attribute}")
        node = nodes[0]
        if attribute == "Name":
            if not isinstance(value, str) or any(ord(c) < 32 for c in value):
                raise SceneError("Tag name must be a string without control characters")
            encoded = value
        elif node.get(attribute) in ("True", "False") or (node.tag in IO_TYPES and attribute == "ForcedValue" and _kind(node.tag) == "bool"):
            encoded = _boolean(value)
        else:
            try:
                if isinstance(value, bool):
                    raise ValueError
                numeric = float(value)
                if not math.isfinite(numeric):
                    raise ValueError
            except (ValueError, TypeError, OverflowError) as exc:
                raise SceneError("Numeric setting must be finite") from exc
            integer = attribute in {"Address", "UpTo"} or (attribute == "ForcedValue" and _kind(node.tag) == "int")
            if integer and not numeric.is_integer():
                raise SceneError("Integer setting must be an integer")
            if attribute in {"Range", "MinTimeToEmit", "MaxTimeToEmit", "Address", "UpTo"} and numeric < 0:
                raise SceneError("This setting cannot be negative")
            encoded = str(int(numeric)) if integer else _fmt(numeric)
        node.set(attribute, encoded)
    emitter = obj.find("Emitter")
    if emitter is not None and float(emitter.get("MinTimeToEmit", "0")) > float(emitter.get("MaxTimeToEmit", "0")):
        raise SceneError("Emitter MinTimeToEmit cannot exceed MaxTimeToEmit")


def patch_plan(plan: dict, patches: list[dict]) -> dict:
    """Return a revised plan; translation requires a position already in the plan.

    Settings syntax is checked here. Source-node existence is checked at build.
    Object, tag, and group UUIDs remain stable for position/settings revisions.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("objects"), list) or not isinstance(patches, list):
        raise SceneError("Expected a plan with objects and a list of patches")
    revised = deepcopy(plan)
    objects = {}
    for obj in revised["objects"]:
        identity = _identity(obj.get("id"), "object id")
        if identity in objects:
            raise SceneError("Duplicate semantic object id")
        objects[identity] = obj
    for patch in patches:
        if not isinstance(patch, dict) or set(patch) - {"object_id", "position", "translate", "yaw_deg", "settings", "tag_names"}:
            raise SceneError("Unknown patch operation")
        if patch.get("object_id") not in objects:
            raise SceneError(f"Unknown patch target: {patch.get('object_id')}")
        obj = objects[patch["object_id"]]
        if "position" in patch and "translate" in patch:
            raise SceneError("Use either position or translate in one patch")
        if "position" in patch:
            obj["position"] = _vector(patch["position"], 3, "position")
        if "translate" in patch:
            if "position" not in obj:
                raise SceneError("Translation requires an explicit plan position")
            obj["position"] = _vector([a+b for a, b in zip(_vector(obj["position"], 3, "position"),
                                                          _vector(patch["translate"], 3, "translate"))], 3, "translated position")
        if "yaw_deg" in patch:
            obj["yaw_deg"] = _number(patch["yaw_deg"], "yaw_deg")
        if "settings" in patch:
            if not isinstance(patch["settings"], list):
                raise SceneError("settings must be a list")
            existing = {(s["path"], s.get("attribute")): s for s in obj.get("settings", [])}
            for setting in patch["settings"]:
                path, attribute, _ = _setting_descriptor(setting)
                existing[path, attribute] = deepcopy(setting)
            obj["settings"] = list(existing.values())
        if "tag_names" in patch:
            if not isinstance(patch["tag_names"], dict):
                raise SceneError("tag_names must be an index-to-name object")
            obj["tag_names"] = {**obj.get("tag_names", {}), **deepcopy(patch["tag_names"])}
    return revised


def _set_vector(proxy: ET.Element, name: str, values: list[float], axes: str = "XYZ") -> None:
    node = proxy.find(name)
    if node is None:
        raise SceneError(f"Source has no {name} transform node")
    for axis, value in zip(axes, values):
        node.set(axis, _fmt(value))


def _read_catalog(catalog: dict) -> tuple[dict, dict]:
    if catalog.get("schema_version") != 1:
        raise SceneError("Unsupported catalog schema_version")
    sources, roots = {}, {}
    for source in catalog.get("sources", []):
        if source["id"] in sources:
            raise SceneError("Duplicate catalog source id")
        data, root = _load(Path(source["path"]))
        if _sha(data) != source["sha256"]:
            raise SceneError(f"Catalog source hash changed: {source['path']}")
        if not _validate_root(root)["passed"]:
            raise SceneError(f"Catalog source has invalid structure: {source['path']}")
        sources[source["id"]], roots[source["id"]] = source, root
    return sources, roots


def _contract(root: ET.Element, objects: list[dict], digest: str) -> dict:
    drivers = root.find("Drivers")
    tags = [{**tag, "object_id": obj["id"]} for obj in objects for tag in obj["tags"]]
    tags += _system_tags(drivers)
    keyed = {tag["key"]: tag for tag in tags}
    mappings = []
    modbus = drivers.find("ModbusTCPClient") if drivers is not None else None
    if modbus is not None:
        offsets = modbus.find("PointIOOffset")
        for node in modbus:
            if "PointIOKey" not in node.attrib:
                continue
            family, direction, index = CHANNEL.fullmatch(node.tag).groups()
            target = keyed[node.get("PointIOKey")]
            offset = int(offsets.get(family + direction + "Offset", "0")) if offsets is not None else 0
            mappings.append({"channel": node.tag, "tag_key": target["key"], "tag_id": target["id"],
                             "kind": target["kind"], "data_type": target["data_type"], "type": target["type"], "channel_family": family,
                             "direction": direction, "channel_index": int(index), "offset": offset})
    mapped = {entry["tag_key"] for entry in mappings}
    return {"schema_version": 1, "scene_sha256": digest, "driver": "ModbusTCPClient",
            "tags": tags, "mappings": mappings,
            "unmapped_tag_keys": [tag["key"] for tag in tags if tag["key"] not in mapped],
            "driver_properties": dict(modbus.find("Properties").attrib)
                if modbus is not None and modbus.find("Properties") is not None else {},
            "channel_counts": {k: int(v) for k, v in modbus.find("PointIOCount").attrib.items()}
                if modbus is not None and modbus.find("PointIOCount") is not None else {},
            "channel_offsets": {k: int(v) for k, v in modbus.find("PointIOOffset").attrib.items()}
                if modbus is not None and modbus.find("PointIOOffset") is not None else {},
            "transport_confirmed": False, "software_load_confirmed": False,
            "supported_limits": _limit_summary(),
            "address_interpretation": "channel_index and configured offset; tag Address is separate"}


def allocate_bool_mappings(objects: list[dict], system_tags: list[dict], *, include_numeric: bool = False) -> list[dict]:
    """Allocate native low contiguous slots; IEC addresses still need verified device binding."""
    counters = {(family,direction):0 for family in ('Bit','Numeric') for direction in ('Input','Output')}
    mappings = []
    if not any(t['system_field'] == 'RunInputKey' for t in system_tags):
        raise SceneError('automatic BOOL mappings require the native Running input')
    mappings.append({'channel': 'BitInput0', 'system_tag': 'RunInputKey'})
    counters['Bit','Input'] = 1
    for obj in sorted(objects, key=lambda o:o['id']):
        for tag in sorted(obj['tags'], key=lambda t:t['index']):
            if tag['data_type'] != 'bool' and not include_numeric:
                raise SceneError('automatic mapping supports only BOOL tags; a verified numeric binding tool is required')
            if tag['data_type'] not in ('bool','int','float'):
                raise SceneError('unsupported tag data type: '+str(tag['data_type']))
            direction = tag['direction']
            family='Bit' if tag['data_type']=='bool' else 'Numeric'
            mappings.append({'channel':f'{family}{direction}{counters[family,direction]}', 'object_id':obj['id'], 'tag_index':tag['index']})
            counters[family,direction] += 1
    return mappings


def apply_process_constraints(root, objects, execution):
    """Apply validated controller ownership/one-item constraints; never force physical inputs."""
    if not execution:
        return None
    feed = execution.get('feed', {})
    ownership_only = execution == {'control_owner':'plc'}
    if not ownership_only and (execution.get('tracking_mode') != 'single_item' or execution.get('max_in_flight') != 1 or \
            feed.get('control') != 'plc' or type(feed.get('items_per_enable')) is not int or feed['items_per_enable'] != 1):
        raise SceneError('unsupported explicit process constraint')
    changes = []
    found = ownership_only
    for obj, metadata in zip(root.findall('Object'), objects):
        if metadata['id'] == feed.get('object_id'):
            emitter = obj.find('Emitter')
            if metadata['prefab'] != 'Emitter' or emitter is None or 'UpTo' not in emitter.attrib:
                raise SceneError('process feed does not reference the inspected Emitter.UpTo setting')
            changes.append({'object_id':metadata['id'], 'path':'Emitter', 'attribute':'UpTo',
                            'previous':emitter.get('UpTo'), 'value':'1', 'authority':'execution_contract.feed'})
            emitter.set('UpTo','1'); found = True
        # With the PLC owning component outputs, release template overrides on their actual tags.
        # Inputs remain byte-for-byte as modeled: forcing a sensor cannot discharge a process check.
        for index, tag in enumerate(_tag_nodes(obj)):
            if _direction(tag.tag) == 'Output':
                if 'UseForcedValue' not in tag.attrib:
                    raise SceneError('native output force status missing; ownership cannot be established')
                changes.append({'object_id':metadata['id'], 'tag_index':index, 'tag_key':tag.get('Key'),
                                'attribute':'UseForcedValue', 'previous':tag.get('UseForcedValue'),
                                'value':'False', 'authority':'PLC component output ownership'})
                tag.set('UseForcedValue','False')
    if not found: raise SceneError('process feed object is absent')
    return {'execution_contract_sha256':_sha(json.dumps(execution,sort_keys=True,ensure_ascii=False,
                         separators=(',', ':'),allow_nan=False).encode('utf-8')), 'changes':changes,
            'physical_input_forcing':False}


def validate_component_settings(objects, catalog):
    """Validate each selected object's exact paths/attributes before any scene is written."""
    templates = {t['template_id']:t for t in catalog['templates']}
    sources = {s['id']:s for s in catalog['sources']}
    roots = {}
    for obj in objects:
        template = templates.get(obj.get('template_id'))
        if template is None: raise SceneError('unknown component template')
        source = sources[template['source_id']]
        if source['id'] not in roots:
            content = Path(source['path']).read_bytes()
            if _sha(content) != source['sha256']: raise SceneError('component source changed during configuration')
            roots[source['id']] = ET.fromstring(content)
        component = deepcopy(roots[source['id']].findall('Object')[template['ordinal']-1])
        _apply_settings(component, obj.get('settings', []))


def build_scene(plan: dict, catalog: dict, output: Path, protocol: dict | None = None,
                *, auto_bool_mappings: bool = False, auto_io_mappings: bool = False, execution_contract: dict | None = None) -> dict:
    """Write a checked scene, returning manifest/IO contract/structural validation.

    yaw_deg is an absolute yaw about +Y, with zero pitch/roll. An explicit mappings
    list replaces all Modbus object mappings. Inherited system mappings remain
    unless their channels are explicitly reused. Duplicate clones inherit no
    ambiguous object mapping; bind them explicitly by semantic object id.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("objects"), list):
        raise SceneError("Plan requires an objects list")
    if set(plan) - {'scene_id', 'base_source_id', 'objects', 'mappings', 'handoff_id'}:
        raise SceneError('Unsupported scene-plan fields; use only scene_id, base_source_id, objects, mappings and handoff_id')
    scene_id = _identity(plan.get("scene_id"), "scene_id")
    sources, roots = _read_catalog(catalog)
    base_id = plan.get("base_source_id")
    if base_id not in roots:
        raise SceneError("Unknown base_source_id")
    output = Path(output).resolve()
    for source in sources.values():
        path = Path(source["path"]).resolve()
        if os.path.normcase(str(output)) == os.path.normcase(str(path)) or (
                output.exists() and os.path.samefile(output, path)):
            raise SceneError("Output must never overwrite a catalog source")
    templates = {}
    for template in catalog.get("templates", []):
        if template["template_id"] in templates:
            raise SceneError("Duplicate catalog template_id")
        templates[template["template_id"]] = template
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, "factoryio-scene/" + scene_id)
    root = deepcopy(roots[base_id])
    insertion = next((i for i, child in enumerate(root) if child.tag == "Object"),
                     next((i for i, child in enumerate(root) if child.tag in {"Instructor", "Drivers", "Screenshot"}), len(root)))
    for obj in list(root.findall("Object")):
        root.remove(obj)
    screenshots = root.findall("Screenshot")
    for screenshot in screenshots:
        root.remove(screenshot)
    # Native Scene.GetSceneData removes all three nodes without a null check.
    # Keep the envelope even when no current thumbnail exists: omitting Screenshot
    # allows a first load but breaks the next native unsaved-change check.
    ET.SubElement(root, "Screenshot", {"Data": ""})
    if root.find("Description") is None:
        ET.SubElement(root, "Description", {"Data": ""})
    if root.find("Camera") is None:
        ET.SubElement(root, "Camera")
    manifest_objects, semantic_ids = [], set()
    inherited = defaultdict(list)
    used_indices = defaultdict(set)
    for specification in plan["objects"]:
        if not isinstance(specification, dict):
            raise SceneError("Each planned object must be an object")
        if set(specification) - {"id", "template_id", "position", "yaw_deg", "tag_names", "settings"}:
            raise SceneError("Unknown planned object field")
        identity = _identity(specification.get("id"), "object id")
        if identity in semantic_ids:
            raise SceneError(f"Duplicate semantic object id: {identity}")
        semantic_ids.add(identity)
        template = templates.get(specification.get("template_id"))
        if template is None:
            raise SceneError(f"Unknown template_id: {specification.get('template_id')}")
        source_id = template["source_id"]
        if source_id not in sources or template["source_sha256"] != sources[source_id]["sha256"]:
            raise SceneError("Template does not match a verified catalog source")
        ordinal = template.get("ordinal")
        source_objects = roots[source_id].findall("Object")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or not 1 <= ordinal <= len(source_objects):
            raise SceneError("Template ordinal is invalid")
        obj = deepcopy(source_objects[ordinal - 1])
        proxy = obj.find("Proxy")
        if proxy.get("PrefabName") != template["prefab"] or proxy.get("Key") != template.get("source_object_key", proxy.get("Key")):
            raise SceneError("Template identity does not match its exact source object")
        for node in obj.iter():
            if "Key" in node.attrib:
                old_key = node.get("Key")
                node.set("Key", str(uuid.uuid5(namespace, "object/" + identity + "/key/" + old_key)))
                if source_id == base_id and node.tag in IO_TYPES:
                    inherited[old_key].append(node.get("Key"))
        group = obj.get("GroupKey")
        if group and group != ZERO_UUID:
            obj.set("GroupKey", str(uuid.uuid5(namespace, "group/" + source_id + "/" + group)))
        if "position" in specification:
            position = _vector(specification['position'], 3, 'position')
            if any(not float(value).is_integer() for value in position):
                raise SceneError('Factory I/O Position uses integer grid coordinates; fractional XYZ is not supported')
            _set_vector(proxy, "Position", position)
        if "yaw_deg" in specification:
            degrees = _number(specification['yaw_deg'], 'yaw_deg') % 360
            if abs(degrees / 90 - round(degrees / 90)) > 1e-9:
                raise SceneError('Factory I/O grid Forward/Up/Right require integer axes; yaw_deg must be a multiple of 90 in this writer')
            yaw = math.radians(degrees)
            q = [0.0, math.sin(yaw / 2), 0.0, math.cos(yaw / 2)]
            _set_vector(proxy, "Rotation", q, "XYZW")
            for name, vector in _basis(q).items():
                _set_vector(proxy, name, vector)
            if proxy.find("FineRotation") is not None:
                proxy.find("FineRotation").text = "0"
        tags = _tag_nodes(obj)
        names = specification.get("tag_names", {})
        if not isinstance(names, dict):
            raise SceneError("tag_names must be a string-index-to-name object")
        for index, name in names.items():
            if not isinstance(index, str) or not re.fullmatch(r"0|[1-9][0-9]*", index) or int(index) >= len(tags):
                raise SceneError(f"Unknown tag index: {index}")
            if not isinstance(name, str) or any(ord(c) < 32 for c in name):
                raise SceneError("Tag name must be a string without control characters")
            tags[int(index)].set("Name", name)
        _apply_settings(obj, specification.get("settings", []))
        component = obj.find("ComponentIO")
        if component is not None and int(component.get("Index", "-1")) >= 0:
            prefab = proxy.get("PrefabName")
            index = int(component.get("Index"))
            if index in used_indices[prefab]:
                index = max(used_indices[prefab], default=0) + 1
                component.set("Index", str(index))
            used_indices[prefab].add(index)
        root.insert(insertion + len(manifest_objects), obj)
        manifest_objects.append({"id": identity, "key": proxy.get("Key"), "prefab": proxy.get("PrefabName"),
                                 "template_id": template["template_id"], "source_id": source_id,
                                 "position": _xml_vector(proxy.find("Position"), "XYZ"),
                                 "rotation": _xml_vector(proxy.find("Rotation"), "XYZW"),
                                 "group_key": obj.get("GroupKey"),
                                 "configuration": {"object_attributes": dict(obj.attrib),
                                     "nodes": [_compact(c) for c in obj if c.tag != "Proxy"]},
                                 "tags": [{**_tag(node, index), "id": f"{identity}/tag/{index}"}
                                          for index, node in enumerate(tags)]})
    process_constraints = apply_process_constraints(root, manifest_objects, execution_contract)
    native_address_changes = _isolate_native_addresses(root)
    # Refresh metadata from exactly the XML which will be loaded by the software.
    for obj, metadata in zip(root.findall('Object'), manifest_objects):
        metadata['configuration'] = {'object_attributes': dict(obj.attrib),
                                     'nodes': [_compact(c) for c in obj if c.tag != 'Proxy']}
        metadata['tags'] = [{**_tag(node, index), 'id': f"{metadata['id']}/tag/{index}"}
                            for index,node in enumerate(_tag_nodes(obj))]
    drivers = root.find("Drivers")
    automatic_mappings=auto_bool_mappings or auto_io_mappings
    allocated_mappings = allocate_bool_mappings(manifest_objects, _system_tags(drivers),include_numeric=auto_io_mappings) if automatic_mappings else None
    if automatic_mappings and 'mappings' in plan:
        raise SceneError('automatic BOOL mapping is enabled; omit model-authored mappings')
    requested_mappings = allocated_mappings if automatic_mappings else plan.get('mappings')
    system_keys = {tag["key"] for tag in _system_tags(drivers)}
    removed, ambiguous = [], []
    if drivers is not None:
        for driver in drivers:
            for node in list(driver):
                if "PointIOKey" not in node.attrib:
                    continue
                old_key = node.get("PointIOKey")
                if old_key in system_keys:
                    continue
                choices = inherited.get(old_key, [])
                if len(choices) == 1:
                    node.set("PointIOKey", choices[0])
                else:
                    driver.remove(node)
                    entry = {"driver": driver.tag, "channel": node.tag, "old_tag_key": old_key}
                    removed.append(entry)
                    if len(choices) > 1:
                        ambiguous.append(entry)
    replaced_systems = []
    if requested_mappings is not None:
        if drivers is None or drivers.find("ModbusTCPClient") is None:
            raise SceneError("Base source has no ModbusTCPClient driver to configure")
        if not isinstance(requested_mappings, list):
            raise SceneError("mappings must be a list")
        modbus = drivers.find("ModbusTCPClient")
        tags_by_object = {obj["id"]: obj["tags"] for obj in manifest_objects}
        selected, channels, keys = [], set(), set()
        for mapping in requested_mappings:
            if not isinstance(mapping, dict):
                raise SceneError("Each mapping must be an object")
            channel, target_id, index = mapping.get("channel"), mapping.get("object_id"), mapping.get("tag_index")
            if not isinstance(channel, str) or not CHANNEL.fullmatch(channel):
                raise SceneError(f"Invalid mapping channel: {channel}")
            family, direction, channel_number = CHANNEL.fullmatch(channel).groups()
            _xml_integer(channel_number, maximum=MODBUS_CLIENT_CHANNEL_LIMITS[family] - 1)
            if 'system_tag' in mapping:
                if set(mapping) != {'channel', 'system_tag'} or mapping['system_tag'] not in _SYSTEM_TYPES:
                    raise SceneError('Invalid system tag mapping')
                target = next((t for t in _system_tags(drivers) if t['system_field'] == mapping['system_tag']), None)
                if target is None:
                    raise SceneError('System tag is absent from the base source')
            else:
                target_tags = tags_by_object.get(target_id)
                if target_tags is None or isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(target_tags):
                    raise SceneError(f"Unknown mapping object/tag: {target_id}/{index}")
                target = target_tags[index]
            if not _compatible(channel, target):
                raise SceneError(f"Mapping {channel} is incompatible with {target['type']}")
            if channel in channels or target["key"] in keys:
                raise SceneError("Duplicate mapping channel or tag target")
            channels.add(channel)
            keys.add(target["key"])
            selected.append(ET.Element(channel, {"PointIOKey": target["key"]}))
        for node in list(modbus):
            if "PointIOKey" not in node.attrib:
                continue
            if not automatic_mappings and node.get("PointIOKey") in system_keys and node.tag not in channels and node.get('PointIOKey') not in keys:
                selected.append(deepcopy(node))
            elif node.get("PointIOKey") in system_keys:
                replaced_systems.append({"channel": node.tag, "tag_key": node.get("PointIOKey")})
            modbus.remove(node)
        counts = modbus.find("PointIOCount")
        if counts is None:
            raise SceneError("Base ModbusTCPClient lacks PointIOCount")
        for family in ("Bit", "Float", "Int", "Numeric"):
            for direction in ("Input", "Output"):
                indices = [int(CHANNEL.fullmatch(node.tag).group(3)) for node in selected
                           if CHANNEL.fullmatch(node.tag).group(1, 2) == (family, direction)]
                counts.set(family + direction + "Count", str(max(indices, default=-1) + 1))
        insertion_at = next((i for i, n in enumerate(modbus) if n.tag == "Properties"), len(modbus))
        for index, node in enumerate(selected):
            modbus.insert(insertion_at + index, node)
    if protocol is not None:
        # Protocol is an orchestrator contract, not an object setting invented by a model.
        # DriverManager.Driver.ModbusTCPClient=64 in the inspected Factory I/O 2.5.8 assembly.
        if not (protocol.get('name') == 'modbus_tcp' and protocol.get('factoryio_role') == 'client'
                and protocol.get('plc_role') == 'server'):
            raise SceneError('Unsupported scene protocol')
        drivers = root.find('Drivers')
        if drivers is None or drivers.find('ModbusTCPClient') is None:
            raise SceneError('Base source has no native Modbus TCP Client configuration')
        drivers.set('CurrentDriver', '64')
    validation = _validate_root(root)
    if not validation["passed"]:
        raise SceneError(f"Generated scene failed structural validation: {validation['issues']}")
    ET.indent(root, space="  ")
    data = b"\xef\xbb\xbf" + ET.tostring(root, encoding="utf-8", xml_declaration=False) + b"\n"
    digest = _sha(data)
    contract = _contract(root, manifest_objects, digest)
    # Recheck just before writing, avoiding a stale catalog after a long build.
    for source in sources.values():
        if _sha(Path(source["path"]).read_bytes()) != source["sha256"]:
            raise SceneError(f"Catalog source changed during build: {source['path']}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".factoryio-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return {"manifest": {"schema_version": 1, "scene_id": scene_id, "scene_path": str(output),
                         "scene_sha256": digest, "base_source_id": base_id,
                         "objects": manifest_objects, 'native_address_reallocation': native_address_changes,
                         'mapping_allocation': {'policy':'automatic_native_contiguous' if auto_io_mappings else 'automatic_bool_contiguous', 'mappings':allocated_mappings,
                                                'device_binding_verified':False} if automatic_mappings else None,
                         'process_constraint_application':process_constraints,
                         "source_hashes": {key: source["sha256"] for key, source in sources.items()},
                         "screenshot_removed": False, "source_thumbnail_removed": bool(screenshots),
                         "thumbnail_placeholder": True, "generated_rendering_available": False,
                         "removed_driver_mappings": removed, "ambiguous_inherited_mappings": ambiguous,
                         "replaced_system_mappings": replaced_systems,
                         "software_load_confirmed": False, "coordinate_units": "native_scene_grid"},
            "io_contract": contract,
            "validation": {**validation, "scene_path": str(output), "scene_sha256": digest}}
