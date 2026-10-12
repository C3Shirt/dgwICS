"""Bounded ST contract checks and a copied-project CODESYS scripting runner.

The declaration scanner only recognizes simple scalar declarations. Compilation
is performed by CODESYS; the scanner is not an IEC parser or transport verifier.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from .contracts import ContractError, canonical_hash, file_hash, read_json


class PLCError(ContractError):
    pass


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}\Z")
_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}(?:\.[A-Za-z_][A-Za-z0-9_]{0,79}){0,2}\Z")
_RESERVED = frozenset(("PROGRAM FUNCTION FUNCTION_BLOCK END_PROGRAM END_FUNCTION END_FUNCTION_BLOCK "
    "VAR VAR_GLOBAL VAR_INPUT VAR_OUTPUT VAR_IN_OUT VAR_TEMP END_VAR IF THEN ELSE ELSIF END_IF "
    "CASE OF END_CASE FOR TO BY DO END_FOR WHILE END_WHILE REPEAT UNTIL END_REPEAT TRUE FALSE "
    "AND OR XOR NOT MOD AT BOOL REAL LREAL INT DINT SINT LINT UINT UDINT USINT ULINT "
    "BYTE WORD DWORD LWORD STRING WSTRING TIME DATE TYPE END_TYPE CONSTANT RETAIN PERSISTENT").split())
_WINDOWS_RESERVED = frozenset(["CON", "PRN", "AUX", "NUL"] +
    [prefix + str(i) for prefix in ("COM", "LPT") for i in range(1, 10)])
_TYPES = {"bool": {"BOOL"}, "float": {"REAL", "LREAL"},
          "int": {"SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT",
                  "BYTE", "WORD", "DWORD", "LWORD"}}
_PLC_BITS = {"BOOL": 1, "SINT": 8, "USINT": 8, "BYTE": 8, "INT": 16, "UINT": 16,
             "WORD": 16, "DINT": 32, "UDINT": 32, "DWORD": 32, "REAL": 32,
             "LINT": 64, "ULINT": 64, "LWORD": 64, "LREAL": 64}
_DEVICE_BINDING_FIELDS = frozenset({"tag_key", "variable", "device_path", "connector_id",
                                   "manual_iec_address", "channel_type", "bit_size"})
_TRANSPORT_MAPPING_FIELDS = ("channel", "tag_key", "channel_family", "channel_index", "offset",
                             "type", "direction", "kind", "data_type")
_CHANNEL = re.compile(r"(Bit|Float|Int|Numeric)(Input|Output)(0|[1-9][0-9]*)\Z")
_BLOCK = re.compile(r"\bVAR(?:_GLOBAL|_INPUT|_OUTPUT|_IN_OUT|_TEMP|_STAT|_EXTERNAL)?\b(.*?)\bEND_VAR\b", re.I | re.S)
_VAR_START = re.compile(r"\bVAR(?:_GLOBAL|_INPUT|_OUTPUT|_IN_OUT|_TEMP|_STAT|_EXTERNAL)?\b", re.I)
_SIMPLE_DECL = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)"
    r"\s*(?:AT\s+%[A-Za-z0-9_.]+\s*)?:\s*([A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s*:=.*)?\s*$", re.I | re.S)


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise PLCError(message)


def _name(value, label: str) -> str:
    _check(isinstance(value, str) and bool(_IDENTIFIER.fullmatch(value)), label + " must be a safe IEC identifier")
    _check(value.upper() not in _RESERVED | _WINDOWS_RESERVED, label + " is reserved")
    return value


def _code(text, label: str) -> str:
    _check(isinstance(text, str) and 0 < len(text) <= 2_000_000 and "\x00" not in text,
           label + " must be nonempty ST text of at most 2 MB")
    return text


def _mask(text: str) -> str:
    """Mask comments, literals, and attributes without interpreting ST statements."""
    result, i, depth, quote = [], 0, 0, None
    while i < len(text):
        pair = text[i:i + 2]
        if depth:
            if pair == "(*": depth += 1; i += 2; result.append("  "); continue
            if pair == "*)": depth -= 1; i += 2; result.append("  "); continue
            result.append("\n" if text[i] == "\n" else " "); i += 1; continue
        if quote:
            if text[i] == "$" and i + 1 < len(text):
                result.append("  "); i += 2; continue
            if text[i] == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    result.append("  "); i += 2; continue
                quote = None
            result.append("\n" if text[i] == "\n" else " "); i += 1; continue
        if pair == "(*": depth = 1; result.append("  "); i += 2; continue
        if pair == "//":
            end = text.find("\n", i)
            if end == -1: end = len(text)
            result.append(" " * (end - i)); i = end; continue
        if text[i] in ("'", '"'): quote = text[i]; result.append(" "); i += 1; continue
        result.append(text[i]); i += 1
    _check(not depth and quote is None, "Unterminated ST comment or string")
    return re.sub(r"\{[^{}]*\}", " ", "".join(result))


def _declarations(text: str, label: str) -> tuple[dict[str, str], list[str]]:
    masked = _mask(text)
    blocks = list(_BLOCK.finditer(masked))
    _check(len(re.findall(r"\bEND_VAR\b", masked, re.I)) == len(blocks) == len(_VAR_START.findall(masked)),
           label + ": unmatched VAR block")
    symbols, unsupported = {}, []
    for block in blocks:
        body = re.sub(r"^\s*(?:(?:CONSTANT|RETAIN|NON_RETAIN|PERSISTENT)\b\s*)*", "", block.group(1), flags=re.I)
        for statement in body.split(";"):
            if not statement.strip(): continue
            match = _SIMPLE_DECL.fullmatch(statement)
            if not match:
                unsupported.append(statement.strip()[:160]); continue
            for variable in match.group(1).split(","):
                variable = _name(variable.strip(), "declaration variable")
                key = variable.casefold()
                _check(key not in symbols, label + ": duplicate variable " + variable)
                symbols[key] = match.group(2).upper()
    return symbols, unsupported


def check_assignment_targets(program: dict) -> None:
    """Reject undeclared assignment roots in the supported scalar subset, before IDE startup.

    This does not type-check expressions or named function parameters; CODESYS still compiles ST.
    Rich declarations outside the bounded scanner are left to the real compiler.
    """
    symbols, unsupported = _declarations(program['declaration'],program['program_name'])
    if unsupported: return
    known = set(symbols)
    for block in program.get('globals',[]):
        declared, complex_ = _declarations(block['declaration'],block['name'])
        if complex_: return
        known.update(declared); known.add(block['name'].casefold())
    code = _mask(program['implementation'])
    pattern = re.compile(r'(?<![A-Za-z0-9_.])([A-Za-z_][A-Za-z0-9_]*)'
                         r'(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\s*\[[^\]]*\])?\s*:=')
    missing = set(); position = 0; depth = 0
    for match in pattern.finditer(code):
        prefix = code[position:match.start()]
        depth += prefix.count('(') - prefix.count(')')
        position = match.start()
        if depth == 0 and match.group(1).casefold() not in known:
            missing.add(match.group(1))
    _check(not missing,'undeclared assignment targets: '+', '.join(sorted(missing))+
           '; declare these internal variables or use existing declared references')


def _wire_encodings(io_contract: dict, tags: dict) -> dict:
    """Explicit positive 16-bit numeric ranges, separate from logical tag types.

    Restrict values to 0..32767 so the supported experiment is independent of
    the unverified signed interpretation of negative Factory I/O values.
    """
    rows = io_contract.get("wire_encodings", [])
    _check(isinstance(rows, list), "wire_encodings must be an array")
    result = {}
    mappings = {m.get("tag_key"): m for m in io_contract.get("mappings", [])}
    for row in rows:
        _check(isinstance(row, dict) and set(row) == {"tag_key", "format", "scale_factor", "logical_min", "logical_max"},
               "wire encoding requires tag_key, format, scale_factor, logical_min and logical_max")
        key = row["tag_key"]
        _check(key in tags and key not in result and tags[key].get("data_type") in ("int", "float"), "invalid numeric wire encoding tag")
        _check(io_contract.get("driver") == "ModbusTCPClient" and row["format"] == "modbus_positive16",
               "unsupported wire format or driver")
        mapping = mappings.get(key, {})
        _check(mapping.get("channel_family") in ("Numeric", "Float", "Int"), "wire encoding needs a numeric driver mapping")
        values = [row[k] for k in ("scale_factor", "logical_min", "logical_max")]
        _check(all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values),
               "wire encoding values must be finite numbers")
        scale, lower, upper = values
        try:
            expected = float(io_contract.get("driver_properties", {}).get("ScaleFactor", 0)) if tags[key]["data_type"] == "float" else 1.0
        except (TypeError, ValueError, OverflowError) as exc:
            raise PLCError("wire encoding requires a numeric driver ScaleFactor") from exc
        _check(scale > 0 and scale == expected and 0 <= lower < upper and upper * scale <= 32767,
               "wire range/scaling differs from driver or exceeds supported positive 16-bit range")
        _check(tags[key]["data_type"] != "int" or all(float(v).is_integer() for v in (lower, upper)), "integer wire range must use integers")
        result[key] = deepcopy(row)
    return result


def _validate_bindings(bindings, tags: dict, namespaces: dict, encodings: dict) -> list[dict]:
    _check(isinstance(bindings, list), "bindings must be an array")
    keys, variables, result = set(), set(), []
    for item in bindings:
        _check(isinstance(item, dict) and set(item) == {"tag_key", "variable", "plc_type"},
               "binding requires exactly tag_key, variable, and plc_type")
        key, variable, type_ = item.get("tag_key"), item.get("variable"), item.get("plc_type")
        _check(isinstance(key, str) and key in tags, "binding references unknown tag_key")
        _check(key not in keys, "duplicate binding tag_key: " + key)
        _check(isinstance(variable, str) and bool(_VARIABLE.fullmatch(variable)), "invalid qualified binding variable")
        _check(all(part.upper() not in _RESERVED for part in variable.split(".")), "reserved binding variable")
        normalized = variable.casefold()
        allowed = {"WORD", "UINT", "INT"} if key in encodings else _TYPES.get(tags[key].get("data_type"), set())
        _check(isinstance(type_, str) and type_ == type_.upper() and type_ in allowed,
               "binding PLC type is incompatible with tag data_type: " + key)
        # Optional application qualification is syntax only; declaration scope is POU/GVL.
        parts = normalized.split(".")
        if len(parts) == 3: parts = parts[1:]
        if len(parts) == 2:
            declared = namespaces.get(parts[0], {}).get(parts[1])
            symbol = tuple(parts)
        else:
            matches = [(scope_name, scope[parts[0]]) for scope_name, scope in namespaces.items() if parts[0] in scope]
            declared = matches[0][1] if len(matches) == 1 else None
            symbol = (matches[0][0], parts[0]) if len(matches) == 1 else None
        _check(declared is not None, "binding variable is undeclared or ambiguous: " + variable)
        _check(declared == type_, "binding PLC type differs from declaration: " + variable)
        _check(symbol not in variables, "multiple tags bind the same variable: " + variable)
        keys.add(key); variables.add(symbol); result.append(deepcopy(item))
    return result


def validate_program(program: dict, io_contract: dict) -> dict:
    """Reject malformed contracts; return deployment blockers separately from syntax."""
    _check(isinstance(program, dict) and type(program.get("schema_version")) is int and program["schema_version"] == 1,
           "program schema_version must be 1")
    _check(isinstance(io_contract, dict) and type(io_contract.get("schema_version")) is int and io_contract["schema_version"] == 1,
           "I/O contract schema_version must be 1")
    name = _name(program.get("program_name"), "program_name")
    declaration = _code(program.get("declaration"), "declaration")
    _code(program.get("implementation"), "implementation")
    header = re.match(r"\s*PROGRAM\s+([A-Za-z_][A-Za-z0-9_]*)\b", _mask(declaration), re.I)
    _check(header is not None and header.group(1).casefold() == name.casefold(), "declaration must begin with PROGRAM " + name)
    _check(len(re.findall(r"\bPROGRAM\b", _mask(declaration), re.I)) == 1, "declaration has multiple PROGRAM headers")
    scopes, unsupported = {}, []
    scopes[name.casefold()], unsupported = _declarations(declaration, name)
    globals_ = program.get("globals")
    _check(isinstance(globals_, list), "globals must be an array")
    for global_ in globals_:
        _check(isinstance(global_, dict) and set(global_) == {"name", "declaration"}, "global requires name and declaration")
        global_name = _name(global_.get("name"), "global.name")
        _check(global_name.casefold() not in scopes, "duplicate global/program name: " + global_name)
        text = _code(global_.get("declaration"), "global.declaration")
        _check(re.match(r"\s*VAR_GLOBAL\b", _mask(text), re.I) is not None, "global declaration must begin with VAR_GLOBAL")
        scopes[global_name.casefold()], unparsed = _declarations(text, global_name)
        unsupported.extend(unparsed)
    for field in ("unresolved", "notes"):
        _check(isinstance(program.get(field), list) and all(isinstance(s, str) for s in program[field]), field + " must be an array of strings")
    tags_list, mappings = io_contract.get("tags"), io_contract.get("mappings")
    _check(isinstance(tags_list, list) and isinstance(mappings, list), "I/O contract needs tags and mappings arrays")
    tags = {}
    for tag in tags_list:
        _check(isinstance(tag, dict) and isinstance(tag.get("key"), str) and tag["key"] not in tags, "invalid or duplicate I/O tag key")
        _check(tag.get("data_type") in _TYPES, "unsupported I/O tag data_type")
        _check(tag.get("forced") is None or isinstance(tag["forced"], dict), "invalid I/O forced-value descriptor")
        tags[tag["key"]] = tag
    mapped = set()
    for mapping in mappings:
        _check(isinstance(mapping, dict) and mapping.get("tag_key") in tags, "mapping references unknown tag_key")
        _check(mapping["tag_key"] not in mapped, "duplicate mapped tag_key")
        mapped.add(mapping["tag_key"])
    encodings = _wire_encodings(io_contract, tags)
    bindings = _validate_bindings(program.get("bindings"), tags, scopes, encodings)
    assigned = {b["tag_key"] for b in bindings}
    required = {key for key in mapped if tags[key].get("object_id") is not None and
                not (tags[key].get("use_forced_value") is True or (tags[key].get("forced") or {}).get("enabled") is True)}
    missing = sorted(required - assigned)
    untransported = sorted(key for key in assigned - mapped if tags[key].get("object_id") is not None and
        not (tags[key].get("use_forced_value") is True or (tags[key].get("forced") or {}).get("enabled") is True))
    unresolved = list(program["unresolved"])
    numeric = {key for key in mapped if tags[key]["data_type"] in ("int", "float")}
    if io_contract.get("driver") == "ModbusTCPClient" and numeric - set(encodings):
        unresolved.append("Modbus numeric tags lack explicit 16-bit wire encodings: " + ", ".join(sorted(numeric - set(encodings))))
    if missing: unresolved.append("Missing bindings for mapped object tags: " + ", ".join(missing))
    if untransported: unresolved.append("Bindings have no Factory I/O driver mapping: " + ", ".join(untransported))
    transport = io_contract.get("transport_confirmed") is True
    if not transport: unresolved.append("Modbus transport encoding and template mapping are unconfirmed")
    return {"schema_version": 1, "passed": True, "scope": "bounded_scalar_declaration_checks",
            "full_st_parse": False, "program_name": name, "program_sha256": canonical_hash(program),
            "bindings": bindings, "wire_encodings": list(encodings.values()), "required_tag_keys": sorted(required), "missing_tag_keys": missing,
            "unmapped_binding_tag_keys": untransported,
            "optional_system_tag_keys": sorted(key for key in mapped - assigned if tags[key].get("object_id") is None),
            "unsupported_declarations": unsupported, "unresolved": unresolved,
            "transport_confirmed": transport, "deployment_ready": not unresolved}


def _qualified_variable(variable: str, application_name: str) -> str:
    _check(isinstance(variable, str) and bool(_VARIABLE.fullmatch(variable)), "invalid verified binding variable")
    parts = variable.split(".")
    _check(len(parts) in (2, 3), "deployment binding variable must name its POU/GVL")
    if len(parts) == 3:
        _check(parts[0].casefold() == application_name.casefold(), "verified binding names a different Application")
        return variable.casefold()
    return (application_name + "." + variable).casefold()


def transport_fingerprint(io_contract: dict) -> str:
    """Hash only transport configuration, independent of scene geometry/evidence.

    A digest identifies a complete contract; it does not itself verify that the
    Factory I/O driver and PLC exchange values with the stated encoding.
    """
    _check(isinstance(io_contract, dict), "transport fingerprint requires an I/O contract")
    protocol = io_contract.get("protocol")
    _check(isinstance(protocol, dict), "transport protocol is missing")
    _check(protocol.get("name") == "modbus_tcp" and protocol.get("factoryio_role") == "client" and protocol.get("plc_role") == "server",
           "transport requires Modbus TCP Factory I/O client / PLC server roles")
    host = protocol.get("host")
    _check(isinstance(host, str) and bool(host.strip()) and not any(ord(c) < 32 for c in host), "transport protocol.host is missing or invalid")
    _check(type(protocol.get("port")) is int and 1 <= protocol["port"] <= 65535, "transport protocol.port is missing or invalid")
    _check(type(protocol.get("unit_id")) is int and 0 <= protocol["unit_id"] <= 255, "transport protocol.unit_id is missing or invalid")
    _check(io_contract.get("driver") == "ModbusTCPClient", "transport driver is missing or unsupported")
    properties = io_contract.get("driver_properties")
    _check(isinstance(properties, dict) and all(key in properties for key in ("ReadCoils", "ReadHoldingRegisters", "ScaleFactor")),
           "transport driver_properties lacks read mode or scaling")
    for field in ("ReadCoils", "ReadHoldingRegisters"):
        _check(type(properties[field]) is bool or properties[field] in ("True", "False"), "transport driver read mode is invalid")
    try:
        scale = float(properties["ScaleFactor"])
    except (ValueError, TypeError, OverflowError) as exc:
        raise PLCError("transport ScaleFactor is invalid") from exc
    _check(not isinstance(properties["ScaleFactor"], bool) and math.isfinite(scale) and scale > 0, "transport ScaleFactor must be finite and positive")
    counts, offsets = io_contract.get("channel_counts"), io_contract.get("channel_offsets")
    _check(isinstance(counts, dict) and bool(counts) and isinstance(offsets, dict) and bool(offsets), "transport channel counts/offsets are missing")
    for label, values, suffix in (("counts", counts, "Count"), ("offsets", offsets, "Offset")):
        for key, value in values.items():
            _check(isinstance(key, str) and re.fullmatch(r"(?:Bit|Float|Int|Numeric)(?:Input|Output)" + suffix, key) is not None and
                   type(value) is int and value >= 0, "transport channel " + label + " contains an invalid field")
    tags_list, mappings = io_contract.get("tags"), io_contract.get("mappings")
    _check(isinstance(tags_list, list) and isinstance(mappings, list) and bool(mappings), "transport tags/mappings are missing")
    tags = {}
    for tag in tags_list:
        _check(isinstance(tag, dict) and isinstance(tag.get("key"), str) and tag["key"] and tag["key"] not in tags,
               "transport tag key is invalid or duplicated")
        tags[tag["key"]] = tag
    channels, keys, normalized_mappings = set(), set(), []
    for mapping in mappings:
        _check(isinstance(mapping, dict) and all(field in mapping for field in _TRANSPORT_MAPPING_FIELDS), "transport mapping lacks slot/index/offset/type/direction fields")
        channel, key = mapping["channel"], mapping["tag_key"]
        match = _CHANNEL.fullmatch(channel) if isinstance(channel, str) else None
        _check(match is not None and channel not in channels, "transport channel is invalid or duplicated")
        _check(isinstance(key, str) and key in tags and key not in keys, "transport mapped tag is unresolved or duplicated")
        family, direction, index = match.groups()
        _check(mapping["channel_family"] == family and mapping["direction"] == direction and mapping["kind"] == direction.lower(),
               "transport slot family/direction/kind fields disagree")
        _check(type(mapping["channel_index"]) is int and mapping["channel_index"] == int(index), "transport slot and channel_index disagree")
        count_key, offset_key = family + direction + "Count", family + direction + "Offset"
        _check(count_key in counts and offset_key in offsets and int(index) < counts[count_key], "transport mapped channel lacks valid count/offset configuration")
        _check(type(mapping["offset"]) is int and mapping["offset"] == offsets[offset_key], "transport mapping offset disagrees with channel_offsets")
        type_ = mapping["type"]
        data_type = mapping["data_type"]
        allowed = {"Bit": {"bool"}, "Float": {"float"}, "Int": {"int"}, "Numeric": {"int", "float"}}
        tag = tags[key]
        _check(isinstance(type_, str) and type_.endswith(direction) and data_type in allowed[family], "transport mapping type is incompatible with slot")
        _check(tag.get("kind") == mapping["kind"] and tag.get("data_type") == data_type, "transport tag kind/data_type differs from mapping")
        normalized = {field: deepcopy(mapping[field]) for field in _TRANSPORT_MAPPING_FIELDS}
        # Future explicit slot/index encodings also invalidate earlier evidence.
        for field in ("slot", "index"):
            if field in mapping: normalized[field] = deepcopy(mapping[field])
        normalized_mappings.append(normalized)
        channels.add(channel); keys.add(key)
    payload = {"protocol": deepcopy(protocol), "driver": io_contract["driver"],
               "mappings": sorted(normalized_mappings, key=lambda row: (row["channel"], row["tag_key"])),
               "tags": [{"key": key, "kind": tags[key]["kind"], "data_type": tags[key]["data_type"]} for key in sorted(keys)],
               "driver_properties": deepcopy(properties), "channel_counts": deepcopy(counts), "channel_offsets": deepcopy(offsets)}
    if "wire_encodings" in io_contract:
        payload["wire_encodings"] = sorted(_wire_encodings(io_contract, tags).values(), key=lambda row: row["tag_key"])
    try:
        return canonical_hash(payload)
    except (ValueError, TypeError, OverflowError) as exc:
        raise PLCError("transport fields must contain finite JSON values") from exc


def validate_device_bindings(value, bindings: list[dict], io_contract: dict, application_name: str) -> list[dict]:
    """Validate evidence descriptors; the bridge must still match actual mappings.

    bit_size may be the decimal string emitted by inspection or a positive int.
    Device paths and connector IDs are literal ScriptEngine tree identifiers.
    """
    _check(isinstance(value, list) and bool(value), "verified_device_bindings must explicitly cover program bindings")
    program_bindings = {b["tag_key"]: b for b in bindings}
    tags = {tag["key"]: tag for tag in io_contract["tags"]}
    keys, variables, endpoints, result = set(), set(), set(), []
    for row in value:
        _check(isinstance(row, dict) and set(row) == _DEVICE_BINDING_FIELDS,
               "verified_device_bindings row requires tag_key, variable, device_path, connector_id, manual_iec_address, channel_type, bit_size")
        key = row["tag_key"]
        _check(isinstance(key, str) and key in program_bindings, "verified device binding references unknown program tag")
        _check(key not in keys, "duplicate verified device binding tag_key")
        variable = _qualified_variable(row["variable"], application_name)
        _check(variable == _qualified_variable(program_bindings[key]["variable"], application_name), "verified variable differs from program binding")
        _check(variable not in variables, "duplicate verified device binding variable")
        for field in ("device_path", "connector_id", "manual_iec_address"):
            text = row[field]
            _check(isinstance(text, str) and bool(text) and len(text) <= 1024 and not any(ord(c) < 32 for c in text),
                   "verified " + field + " must be an explicit inspection value")
        _check(all(part not in ("", ".", "..") for part in row["device_path"].split("/")), "invalid verified device tree path")
        address = row["manual_iec_address"]
        _check(re.fullmatch(r"%[IQM][XBWDL]?[0-9]+(?:\.[0-9]+)*", address) is not None,
               "verified manual_iec_address must be a concrete IEC address")
        direction = tags[key].get("direction") or str(tags[key].get("kind", "")).capitalize()
        _check(direction in ("Input", "Output") and row["channel_type"] == direction,
               "verified channel_type differs from the I/O direction")
        _check(address.startswith("%I" if direction == "Input" else "%Q"), "verified IEC address direction differs")
        bits = row["bit_size"]
        _check((type(bits) is int and bits > 0) or (isinstance(bits, str) and re.fullmatch(r"[1-9][0-9]*", bits) is not None),
               "verified bit_size must be a positive integer or decimal string")
        _check(int(bits) == _PLC_BITS[program_bindings[key]["plc_type"]], "verified bit_size differs from PLC scalar type")
        endpoint = (row["device_path"], row["connector_id"], address)
        _check(endpoint not in endpoints, "duplicate verified device binding channel/address")
        normalized = deepcopy(row); normalized["bit_size"] = str(bits)
        keys.add(key); variables.add(variable); endpoints.add(endpoint); result.append(normalized)
    _check(keys == set(program_bindings), "verified_device_bindings does not cover every program binding")
    return result


def _write_text(path: Path, text: str) -> None:
    # Replacement detaches any existing hardlink; never truncate an existing inode.
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text.rstrip() + "\n")
    temporary.replace(path)


def export_program(program: dict, io_contract: dict, output: Path) -> dict:
    report = validate_program(program, io_contract)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    _write_text(output / "program.json", json.dumps(program, ensure_ascii=False, indent=2, allow_nan=False))
    _write_text(output / (program["program_name"] + ".declaration.st"), program["declaration"])
    _write_text(output / (program["program_name"] + ".implementation.st"), program["implementation"])
    for global_ in program["globals"]:
        _write_text(output / (global_["name"] + ".declaration.st"), global_["declaration"])
    _write_text(output / "binding-report.json", json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return report


def run_codesys(config: dict, output: Path, program: dict | None = None,
                io_contract: dict | None = None, deploy: bool = False,
                commissioning: bool = False) -> dict:
    """Compile a fresh baseline copy and require a nonce/hash-verified bridge result."""
    result = {"schema_version": 1, "backend": "scripting", "compiled": False, "started": False,
              "bindings_verified": False, "task_program_verified": False,
              "transport_sha256": None, "transport_verified": False,
              "diagnostics": [], "project_path": None, "source_sha256": None, "project_sha256": None,
              "deploy_requested": bool(deploy)}
    source = None
    def diagnostic(code, message): result["diagnostics"].append({"code": code, "message": str(message)})
    try:
        _check(isinstance(config, dict) and config.get("backend", "scripting") == "scripting", "unsupported CODESYS backend")
        _check(type(deploy) is bool, "deploy must be boolean")
        _check(type(commissioning) is bool and (not commissioning or deploy), "commissioning requires deploy=true")
        result["commissioning"] = commissioning
        result["physical_acceptance_passed"] = False
        online_probe = config.get("online_probe")
        if online_probe is not None:
            online_probe = dict(online_probe) if isinstance(online_probe, dict) else online_probe
            _check(isinstance(online_probe, dict) and set(online_probe) <= {"connect", "host", "port", "gateway_name", "device_name", "expressions", "action", "reset_mode"},
                   "online_probe contains unsupported options")
            action = online_probe.get('action')
            _check(action in (None, 'stop', 'start', 'reset'), 'unsupported PLC lifecycle action')
            _check(action is None or online_probe.get('connect') is True, 'PLC lifecycle action requires online login')
            _check('reset_mode' not in online_probe or action == 'reset', 'reset_mode requires reset action')
            _check(action != 'reset' or online_probe.get('reset_mode') in ('warm', 'cold'), 'reset supports warm/cold only')
            _check(type(online_probe.get("connect", False)) is bool and not deploy,
                   "online_probe requires boolean connect and deploy=false")
            _check(online_probe.get("host", "127.0.0.1") == "127.0.0.1", "online_probe target must be loopback")
            _check(type(online_probe.get("port", 11740)) is int and 1 <= online_probe.get("port", 11740) <= 65535,
                   "online_probe requires a TCP port 1..65535")
            credentials = config.get("online_credentials")
            if credentials is not None:
                _check(isinstance(credentials, dict) and set(credentials) == {"username_env", "password_env"},
                       "online_credentials requires username_env and password_env references")
                _check(all(isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value)
                           for value in credentials.values()), "online_credentials requires environment variable names")
                _check(credentials["username_env"] != credentials["password_env"], "credential environment names must differ")
                online_probe["credential_env"] = dict(credentials)
        output = Path(output).resolve()
        output.mkdir(parents=True, exist_ok=True)
        template = config.get("template_project")
        _check(isinstance(template, str) and bool(template), "template_project must be configured")
        source = Path(template).resolve(strict=True)
        _check(source.is_file() and source.suffix.lower() == ".project", "template must be a .project file")
        result["source_path"], result["source_sha256"] = str(source), file_hash(source)
        project_dir, request_path, result_path = output / "project", output / "request.json", output / "result.json"
        _check(not project_dir.exists() and not project_dir.is_symlink(), "project output already exists; use a fresh output directory")
        _check(not request_path.exists() and not result_path.exists() and not request_path.is_symlink() and not result_path.is_symlink(),
               "bridge request/result already exists; use a fresh output directory")
        # This must happen before CODESYS opens the project or any ST is exported.
        project_dir.mkdir()
        project_path = project_dir / source.name
        with source.open("rb") as original, project_path.open("xb") as copy:
            shutil.copyfileobj(original, copy)
        _check(not os.path.samefile(source, project_path), "copied project aliases original")
        _check(file_hash(project_path) == result["source_sha256"], "template changed while copying")
        result["project_path"], result["project_sha256"] = str(project_path), file_hash(project_path)
        report, verified_device_bindings = None, []
        application_name = _name(config.get("application_name", "Application"), "application_name")
        expected_task_program = _name(config.get("expected_task_program", config.get("program_name", "PLC_PRG")), "expected_task_program")
        if program is not None:
            _check(io_contract is not None, "program requires an I/O contract")
            report = export_program(program, io_contract, output)
            _check(program["program_name"].casefold() == config.get("program_name", "PLC_PRG").casefold(), "configured program_name differs")
            result["binding_report"] = report
        deployment_report = report
        if commissioning and report:
            # Physical observations require a running simulation. Preserve them as
            # pending evidence while all structural/transport/compiler gates remain.
            result["commissioning_pending"] = list(program["unresolved"])
            commissioning_program = deepcopy(program)
            commissioning_program["unresolved"] = []
            deployment_report = validate_program(commissioning_program, io_contract)
        deployment_blockers = list(deployment_report["unresolved"]) if deployment_report else ["Deployment requires a program and I/O contract"]
        if config.get("device_patch") is not None:
            deployment_blockers.append("Device patch must be compiled and verified as a new template before deployment")
        if config.get("transport_confirmed") is not True:
            deployment_blockers.append("CODESYS transport_confirmed must be explicitly true")
        if report:
            try:
                result["transport_sha256"] = transport_fingerprint(io_contract)
            except PLCError as exc:
                deployment_blockers.append(str(exc))
            verified_transport_sha = config.get("verified_transport_sha256")
            result["transport_verified"] = (isinstance(verified_transport_sha, str) and
                re.fullmatch(r"[a-f0-9]{64}", verified_transport_sha) is not None and
                result["transport_sha256"] is not None and verified_transport_sha == result["transport_sha256"])
            if not result["transport_verified"]:
                deployment_blockers.append("verified_transport_sha256 does not match the current I/O transport contract")
            configured = config.get("bindings", [])
            _check(isinstance(configured, list), "configured bindings must be an array")
            if configured != report["bindings"]:
                deployment_blockers.append("Generated bindings do not exactly match verified template bindings in config")
            try:
                verified_device_bindings = validate_device_bindings(config.get("verified_device_bindings"), report["bindings"], io_contract, application_name)
            except PLCError as exc:
                deployment_blockers.append(str(exc))
            if expected_task_program.casefold() != program["program_name"].casefold():
                deployment_blockers.append("expected_task_program must match the generated program_name")
            verified_sha = config.get("verified_template_sha256")
            if not isinstance(verified_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", verified_sha) or verified_sha != result["source_sha256"]:
                deployment_blockers.append("verified_template_sha256 does not match the current baseline source")
        result["deployment_blockers"] = deployment_blockers
        effective_deploy = deploy and not deployment_blockers
        if deploy and deployment_blockers:
            diagnostic("deployment_blocked", "; ".join(deployment_blockers))
        executable = Path(config.get("executable", "G:/codeesys/CODESYS/Common/CODESYS.exe")).resolve(strict=True)
        _check(executable.is_file(), "CODESYS executable must be configured")
        timeout = config.get("timeout_seconds", 120)
        _check(isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and math.isfinite(timeout) and 0 < timeout <= 3600,
               "timeout_seconds must be finite and positive, at most 3600")
        bridge_source = Path(__file__).resolve().with_name("codesys_bridge.py")
        _check(bridge_source.is_file(), "CODESYS bridge is missing")
        bridge = output / "codesys_bridge.py"
        with bridge_source.open("rb") as original, bridge.open("xb") as copy:
            shutil.copyfileobj(original, copy)
        result["bridge_sha256"] = file_hash(bridge)
        request_id = uuid.uuid4().hex
        request = {"schema_version": 1, "request_id": request_id, "project_path": str(project_path),
                   "bridge_sha256": result["bridge_sha256"],
                   "result_path": str(result_path), "inspection_path": str(output / "project-inspection.json"),
                   "source_sha256": result["source_sha256"], "application_name": application_name,
                   "verified_template_sha256": config.get("verified_template_sha256"),
                   "transport_sha256": result["transport_sha256"], "verified_transport_sha256": verified_transport_sha if report else None,
                   "verified_device_bindings": verified_device_bindings, "expected_task_program": expected_task_program,
                   "device_patch": config.get("device_patch"),
                   "configured_inspection_path": str(output / "configured-inspection.json"),
                   "program": program, "online_probe": online_probe, "deploy": effective_deploy, "transport_confirmed": config.get("transport_confirmed") is True,
                   "deployment_online": {"connect": True, "host": "127.0.0.1", "port": 11740,
                       "device_name": "Device", "credential_env": config.get("online_credentials"),
                       "expressions": config.get("monitor_expressions")},
                   "deployment_ready": bool(report and not deployment_blockers)}
        with request_path.open("x", encoding="utf-8") as stream:
            json.dump(request, stream, ensure_ascii=False, indent=2, allow_nan=False)
        command = [str(executable), "--noUI", "--profile=" + config.get("profile", "CODESYS V3.5 SP22 Patch 3"), "--runscript=" + str(bridge)]
        result["command"] = command
        # CODESYS parses the raw Windows command line and requires quotes after
        # '=', while list2cmdline would quote the entire argument containing spaces.
        profile = config.get("profile", "CODESYS V3.5 SP22 Patch 3")
        _check(isinstance(profile, str) and profile and not any(c in profile for c in '\"\r\n'), "invalid profile name")
        launch_command = command
        if os.name == "nt":
            _check(not any(c in str(executable) + str(bridge) for c in '\"\r\n'), "invalid executable or bridge path")
            launch_command = '\"' + str(executable) + '\" --noUI --profile=\"' + profile + '\" --runscript=\"' + str(bridge) + '\"'
        result["launch_command"] = launch_command
        env = os.environ.copy(); env["FACTORYIO_CODESYS_REQUEST"] = str(request_path)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(launch_command, shell=False, cwd=str(output), env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creationflags)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try: stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); stdout, stderr = process.communicate(timeout=10)
            diagnostic("timeout", "CODESYS exceeded timeout; only this spawned process was terminated")
            result["returncode"] = process.returncode
            _write_text(output / "codesys.stdout.log", stdout.decode("utf-8", errors="replace"))
            _write_text(output / "codesys.stderr.log", stderr.decode("utf-8", errors="replace"))
            return result
        result["returncode"] = process.returncode
        _check(file_hash(bridge) == result["bridge_sha256"], "bridge snapshot changed during execution")
        _write_text(output / "codesys.stdout.log", stdout.decode("utf-8", errors="replace"))
        _write_text(output / "codesys.stderr.log", stderr.decode("utf-8", errors="replace"))
        _check(result_path.is_file() and not result_path.is_symlink(), "CODESYS returned no fresh bridge result")
        bridge_result = read_json(result_path)
        _check(isinstance(bridge_result, dict) and bridge_result.get("schema_version") == 1 and bridge_result.get("request_id") == request_id,
               "bridge result is stale or malformed")
        _check(Path(bridge_result.get("project_path", "")).resolve() == project_path.resolve(), "bridge result project differs")
        _check(bridge_result.get("source_sha256") == result["source_sha256"], "bridge source hash differs")
        actual_sha = file_hash(project_path)
        _check(bridge_result.get("project_sha256") == actual_sha, "bridge project hash differs from saved project")
        result["project_sha256"] = actual_sha
        result["bridge_result"] = bridge_result
        if online_probe is not None:
            result["online_probe"] = bridge_result.get("online_probe")
        result["diagnostics"].extend(bridge_result.get("diagnostics", []))
        errors, warnings = bridge_result.get("error_count"), bridge_result.get("warning_count")
        _check(type(errors) is int and errors >= 0 and type(warnings) is int and warnings >= 0, "bridge compiler counts missing or invalid")
        messages = bridge_result.get("messages")
        _check(isinstance(messages, list) and all(isinstance(m, dict) for m in messages), "bridge compiler messages missing")
        result["error_count"], result["warning_count"], result["messages"] = errors, warnings, messages
        result["compiled"] = (bridge_result.get("compiled") is True and bridge_result.get("build_completed") is True and
            bridge_result.get("messages_verified") is True and errors == 0 and
            not any(m.get("severity") in ("error", "unknown") for m in messages) and process.returncode == 0)
        result["bindings_verified"] = bridge_result.get("bindings_verified") is True
        result["task_program_verified"] = bridge_result.get("task_program_verified") is True
        result["started"] = (result["compiled"] and effective_deploy and result["transport_verified"] and result["bindings_verified"] and result["task_program_verified"] and bridge_result.get("started") is True and
                             bridge_result.get("application_state", "").lower() == "run")
        if not result["compiled"]: diagnostic("compile_unconfirmed", "Build completion and zero compiler errors were not verified")
        return result
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        diagnostic("codesys_runner", exc)
        return result
    finally:
        if source is not None and result["source_sha256"] is not None:
            try:
                result["source_unchanged"] = file_hash(source) == result["source_sha256"]
                if not result["source_unchanged"]:
                    result["compiled"] = result["started"] = False
                    diagnostic("source_changed", "Original template changed during this run")
            except OSError as exc:
                result["compiled"] = result["started"] = False
                diagnostic("source_verification", exc)
