"""Read a Factory I/O XML scene and report its observed structure.

Uses only the Python standard library. Never writes the input scene.
This is an inspector of observed files, not a scene writer or an XSD validator.
"""

import argparse
import base64
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import xml.etree.ElementTree as ET
from xml.parsers import expat


IO_TYPES = {
    "BinaryInput", "BinaryOutput", "FloatInput", "FloatOutput",
    "AnalogueInput", "AnalogueOutput", "IntInput", "IntOutput",
}
CHANNEL = re.compile(r"^(Bit|Float|Int|Numeric)(Input|Output)(\d+)$")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_scene(path):
    data = path.read_bytes()
    root = ET.fromstring(data)
    if root.tag != "FactoryIO":
        raise ValueError(f"Unexpected root element in {path}: {root.tag}")
    starts = []
    parser = expat.ParserCreate()
    parser.StartElementHandler = lambda name, attrs: starts.append(
        (name, parser.CurrentLineNumber)
    )
    parser.Parse(data, True)
    elements = list(root.iter())
    if [e.tag for e in elements] != [tag for tag, _ in starts]:
        raise ValueError("XML line indexing does not match element traversal")
    locations = {id(e): line for e, (_, line) in zip(elements, starts)}
    return data, root, locations


def compact(element):
    return {
        "element": element.tag,
        "attributes": dict(element.attrib),
        "text": (element.text or "").strip() or None,
        "children": [compact(c) for c in element],
    }


def inspect_scene(path):
    data, root, locations = load_scene(path)
    objects, tags, proxies = [], {}, defaultdict(list)
    for ordinal, obj in enumerate(root.findall("Object"), 1):
        proxy = obj.find("Proxy")
        io_tags = []
        for tag in obj.iter():
            if tag.tag in IO_TYPES:
                entry = {
                    "type": tag.tag, "attributes": dict(tag.attrib),
                    "line": locations[id(tag)], "object_ordinal": ordinal,
                    "prefab": proxy.get("PrefabName") if proxy is not None else None,
                }
                io_tags.append(entry)
                tags.setdefault(tag.get("Key"), []).append(entry)
        if proxy is not None:
            proxies[proxy.get("Key")].append(ordinal)
        objects.append({
            "ordinal": ordinal, "line": locations[id(obj)],
            "attributes": dict(obj.attrib),
            "proxy": compact(proxy) if proxy is not None else None,
            "component_io": compact(obj.find("ComponentIO"))
                if obj.find("ComponentIO") is not None else None,
            "operating_modes": [compact(m) for m in obj.findall("OperatingMode")],
            "tags": io_tags,
            "extra_configuration": [compact(c) for c in obj
                if c.tag not in ("Proxy", "ComponentIO", "OperatingMode")],
        })

    drivers_node = root.find("Drivers")
    aliases = {}
    drivers = []
    issues = []
    if drivers_node is not None:
        for name, key in drivers_node.attrib.items():
            if name.endswith("Key"):
                aliases.setdefault(key, []).append(name)
        for driver in drivers_node:
            mappings = []
            for channel in driver:
                key = channel.get("PointIOKey")
                if key is None:
                    continue
                candidates = tags.get(key, [])
                alias = aliases.get(key, [])
                match = CHANNEL.fullmatch(channel.tag)
                resolution = "object_tag" if len(candidates) == 1 else (
                    "system_key" if not candidates and alias else "unresolved_or_ambiguous"
                )
                target = candidates[0] if len(candidates) == 1 else None
                family, direction, index = match.groups() if match else (None, None, None)
                offset_node = driver.find("PointIOOffset")
                offset = offset_node.get(f"{family}{direction}Offset") \
                    if offset_node is not None and match else None
                row = {
                    "channel": channel.tag, "point_io_key": key,
                    "line": locations[id(channel)], "resolution": resolution,
                    "family": family, "direction": direction,
                    "channel_index": int(index) if index is not None else None,
                    "configured_offset": int(offset) if offset is not None else None,
                    "tag_name": target["attributes"].get("Name") if target else None,
                    "tag_type": target["type"] if target else None,
                    "scene_tag_address": target["attributes"].get("Address") if target else None,
                    "tag_line": target["line"] if target else None,
                    "prefab": target["prefab"] if target else None,
                    "system_fields": alias,
                }
                if resolution == "unresolved_or_ambiguous":
                    issues.append({"driver": driver.tag, **row})
                mappings.append(row)
            drivers.append({
                "name": driver.tag, "line": locations[id(driver)],
                "attributes": dict(driver.attrib),
                "configuration": [compact(c) for c in driver if "PointIOKey" not in c.attrib],
                "mapping_count": len(mappings), "mappings": mappings,
            })

    thumbnail = None
    screenshot = root.find("Screenshot")
    screenshot_info = None
    if screenshot is not None and screenshot.get("Data"):
        thumbnail = base64.b64decode(screenshot.get("Data"), validate=True)
        png = thumbnail.startswith(b"\x89PNG\r\n\x1a\n")
        screenshot_info = {
            "line": locations[id(screenshot)],
            "base64_characters": len(screenshot.get("Data")),
            "decoded_bytes": len(thumbnail), "sha256": digest(thumbnail),
            "is_png": png,
            "dimensions": list(struct.unpack(">II", thumbnail[16:24])) if png else None,
        }

    attribute_names = defaultdict(set)
    for element in root.iter():
        attribute_names[element.tag].update(element.attrib)
    all_tags = [t for entries in tags.values() for t in entries]
    modbus = next((d for d in drivers if d["name"] == "ModbusTCPClient"), None)
    mapped = {m["point_io_key"] for m in modbus["mappings"]} if modbus else set()
    summary = {
        "source": str(path.resolve()), "byte_count": len(data), "sha256": digest(data),
        "utf8_bom": data.startswith(b"\xef\xbb\xbf"),
        "line_count": len(data.splitlines()), "root_attributes": dict(root.attrib),
        "top_level_counts": dict(Counter(e.tag for e in root)),
        "object_count": len(objects),
        "prefab_counts": dict(Counter(
            o.find("Proxy").get("PrefabName") for o in root.findall("Object")
            if o.find("Proxy") is not None)),
        "tag_counts": dict(Counter(t["type"] for t in all_tags)),
        "driver_count": len(drivers),
        "duplicate_proxy_keys": {k: v for k, v in proxies.items() if len(v) > 1},
        "duplicate_io_keys": {k: len(v) for k, v in tags.items() if len(v) > 1},
        "mapping_reference_issues": issues,
        "forced_tags": [t for t in all_tags if t["attributes"].get("UseForcedValue") == "True"],
        "unmapped_object_tags_in_modbus_client": [t for t in all_tags if t["attributes"].get("Key") not in mapped],
        "element_attribute_names": {k: sorted(v) for k, v in attribute_names.items()},
        "camera": compact(root.find("Camera")) if root.find("Camera") is not None else None,
        "instructor": compact(root.find("Instructor")) if root.find("Instructor") is not None else None,
        "driver_attributes": dict(drivers_node.attrib) if drivers_node is not None else {},
        "objects": objects, "drivers": drivers, "screenshot": screenshot_info,
    }
    return summary, thumbnail


def bounded(value):
    if value is not None and len(value) > 200:
        return {"characters": len(value), "sha256_utf8": digest(value.encode("utf-8"))}
    return value


def compare_scenes(before_path, after_path):
    before_data, before, _ = load_scene(before_path)
    after_data, after, _ = load_scene(after_path)
    differences = []

    def visit(x, y, path):
        if x.tag != y.tag:
            differences.append({"path": path, "kind": "tag", "before": x.tag, "after": y.tag})
            return
        for key in sorted(set(x.attrib) | set(y.attrib)):
            if x.get(key) != y.get(key):
                differences.append({"path": path + "/@" + key, "kind": "attribute",
                    "before": bounded(x.get(key)), "after": bounded(y.get(key))})
        if (x.text or "").strip() != (y.text or "").strip():
            differences.append({"path": path, "kind": "text",
                "before": bounded((x.text or "").strip()), "after": bounded((y.text or "").strip())})
        if len(x) != len(y):
            differences.append({"path": path, "kind": "child_count", "before": len(x), "after": len(y)})
        sibling_counts = Counter()
        for xc, yc in zip(x, y):
            sibling_counts[xc.tag] += 1
            visit(xc, yc, f"{path}/{xc.tag}[{sibling_counts[xc.tag]}]")

    visit(before, after, "/FactoryIO")
    return {
        "before": str(before_path.resolve()), "before_sha256": digest(before_data),
        "after": str(after_path.resolve()), "after_sha256": digest(after_data),
        "comparison": "Element tree; ignores formatting whitespace and attribute order; preserves child order",
        "difference_count": len(differences), "differences": differences,
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--compare", type=Path, help="Reference scene, used as the before side")
    parser.add_argument("--corpus", action="append", type=Path, default=[])
    args = parser.parse_args()
    report, thumbnail = inspect_scene(args.scene)
    comparison = compare_scenes(args.compare, args.scene) if args.compare else None
    corpus = []
    for directory in args.corpus:
        for scene in sorted(directory.glob("*.factoryio")):
            item, _ = inspect_scene(scene)
            corpus.append({key: item[key] for key in (
                "source", "sha256", "root_attributes", "top_level_counts", "object_count",
                "prefab_counts", "tag_counts", "driver_count", "mapping_reference_issues",
                "duplicate_proxy_keys", "duplicate_io_keys", "element_attribute_names",
            )})
    if args.output_dir:
        out = args.output_dir
        if out.resolve() == args.scene.resolve():
            raise ValueError("Output directory must not be the source file")
        out.mkdir(parents=True, exist_ok=True)
        for name, content in (("inspection.json", report), ("comparison.json", comparison), ("corpus-summary.json", corpus)):
            if content is not None:
                (out / name).write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        if thumbnail is not None and report["screenshot"]["is_png"]:
            (out / "thumbnail.png").write_bytes(thumbnail)
        modbus = next((d for d in report["drivers"] if d["name"] == "ModbusTCPClient"), None)
        if modbus and modbus["mappings"]:
            with (out / "modbus-client-mapping.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(modbus["mappings"][0]))
                writer.writeheader()
                writer.writerows(modbus["mappings"])
    unchanged = digest(args.scene.read_bytes()) == report["sha256"]
    if not unchanged:
        raise RuntimeError("Input changed during inspection; reports may be stale")
    print(json.dumps({
        "source": report["source"], "sha256": report["sha256"],
        "object_count": report["object_count"], "tag_counts": report["tag_counts"],
        "driver_count": report["driver_count"],
        "mapping_reference_issues": len(report["mapping_reference_issues"]),
        "comparison_difference_count": comparison["difference_count"] if comparison else None,
        "corpus_file_count": len(corpus), "input_hash_unchanged": unchanged,
        "output_dir": str(args.output_dir.resolve()) if args.output_dir else None,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
