"""Structural scene writer tests; these do not claim an application load test."""

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from factoryio_agents.scene import SceneError, build_catalog, build_scene, patch_plan, validate_scene


FIXTURE = """<FactoryIO Type="MySavedScene" Version="2.5.8" GUID="00000000-0000-0000-0000-000000000000">
<Description Text="fixture"/><Camera><CameraPosition Name="Current"/></Camera>
<Object Locked="False" GroupKey="00000000-0000-0000-0000-000000000099">
 <Proxy PrefabName="DiffusePhotoelectricSensor" Key="00000000-0000-0000-0000-000000000001">
  <Position X="10" Y="5" Z="20"/><Rotation X="0" Y="0" Z="0" W="1"/>
  <FineRotation>0</FineRotation><FinePosition>0</FinePosition>
  <Forward X="0" Y="0" Z="1"/><Up X="0" Y="1" Z="0"/><Right X="1" Y="0" Z="0"/>
 </Proxy><ComponentIO Index="1" CurrentOperatingMode="0"/>
 <OperatingMode Description="observed-mode"><GroupIO Description="Default">
  <BinaryInput Name="Sensor" Address="4" Key="00000000-0000-0000-0000-000000000011" OpenCircuit="False" ShortCircuit="False" UseForcedValue="False" ForcedValue="False"/>
 </GroupIO></OperatingMode><Sensor Range="0.8"/>
</Object>
<Object Locked="False" GroupKey="00000000-0000-0000-0000-000000000099">
 <Proxy PrefabName="RollerConveyor6M" Key="00000000-0000-0000-0000-000000000002">
  <Position X="20" Y="0" Z="20"/><Rotation X="0" Y="0" Z="0" W="1"/>
  <FineRotation>0</FineRotation><FinePosition>0</FinePosition>
  <Forward X="0" Y="0" Z="1"/><Up X="0" Y="1" Z="0"/><Right X="1" Y="0" Z="0"/>
 </Proxy><ComponentIO Index="1" CurrentOperatingMode="0"/>
 <OperatingMode Description="observed-mode"><GroupIO Description="Default">
  <BinaryOutput Name="Motor" Address="9" Key="00000000-0000-0000-0000-000000000012" OpenCircuit="False" ShortCircuit="False" UseForcedValue="False" ForcedValue="False"/>
 </GroupIO></OperatingMode><Encoder Enabled="False"/>
</Object>
<Instructor RunModeOnly="False"/>
<Drivers CurrentDriver="0" RunInputKey="00000000-0000-0000-0000-000000000090">
 <ModbusTCPClient>
  <PointIOCount BitInputCount="10" BitOutputCount="1" FloatInputCount="0" FloatOutputCount="0" IntInputCount="0" IntOutputCount="0" NumericInputCount="0" NumericOutputCount="0"/>
  <PointIOOffset BitInputOffset="12" BitOutputOffset="20" FloatInputOffset="0" FloatOutputOffset="0" IntInputOffset="0" IntOutputOffset="0" NumericInputOffset="0" NumericOutputOffset="0"/>
  <BitInput0 PointIOKey="00000000-0000-0000-0000-000000000011"/>
  <BitInput9 PointIOKey="00000000-0000-0000-0000-000000000090"/>
  <BitOutput0 PointIOKey="00000000-0000-0000-0000-000000000012"/>
  <Properties ReadCoils="False" ReadHoldingRegisters="False" ScaleFactor="100"/>
 </ModbusTCPClient>
 <OtherDriver>
  <PointIOCount BitInputCount="1" BitOutputCount="1"/>
  <PointIOOffset BitInputOffset="0" BitOutputOffset="0"/>
  <BitInput0 PointIOKey="00000000-0000-0000-0000-000000000011"/>
  <BitOutput0 PointIOKey="00000000-0000-0000-0000-000000000012"/>
 </OtherDriver>
</Drivers><Screenshot Data="stale-thumbnail-content"/>
</FactoryIO>"""


class SceneTests(unittest.TestCase):
    def test_generic_mapping_accepts_numeric_signals_without_assuming_a_transport_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'numeric.factoryio'
            xml=ET.fromstring(FIXTURE);sensor=xml.find('.//BinaryInput');sensor.tag='AnalogueInput';sensor.set('ForcedValue','0')
            for driver in xml.find('Drivers'):
                driver.find('BitInput0').tag='NumericInput0'
                driver.find('PointIOCount').set('NumericInputCount','1')
                driver.find('PointIOOffset').set('NumericInputOffset','0')
            source.write_text(ET.tostring(xml,encoding='unicode'),encoding='utf-8');catalog=build_catalog([source])
            plan={'scene_id':'numeric','base_source_id':catalog['sources'][0]['id'],
                  'objects':[{'id':'process_sensor','template_id':catalog['templates'][0]['template_id'],'position':[10,5,20]}]}
            result=build_scene(plan,catalog,root/'generated.factoryio',auto_io_mappings=True)
            mappings=result['io_contract']['mappings']
            self.assertTrue(result['validation']['passed'])
            self.assertEqual({m['channel_family'] for m in mappings},{'Bit','Numeric'})
            sensor_key=next(t['key'] for t in result['io_contract']['tags'] if t.get('object_id')=='process_sensor')
            self.assertEqual(next(m['channel'] for m in mappings if m['tag_key']==sensor_key),'NumericInput0')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "source.factoryio"
        self.source.write_text(FIXTURE, encoding="utf-8")
        self.source_bytes = self.source.read_bytes()
        self.catalog = build_catalog([self.source])
        self.plan = {"scene_id": "test-scene", "base_source_id": self.catalog["sources"][0]["id"],
                     "objects": [{"id": "sensor", "template_id": self.catalog["templates"][0]["template_id"],
                                  "position": [10, 5, 20]},
                                 {"id": "conveyor", "template_id": self.catalog["templates"][1]["template_id"]}]}
        self.output = self.directory / "output.factoryio"

    def test_catalog_is_json_safe_and_excludes_screenshot(self):
        serialized = json.dumps(self.catalog)
        self.assertNotIn("stale-thumbnail", serialized)
        self.assertNotIn("Screenshot", serialized)
        self.assertEqual(self.catalog, build_catalog([self.source]))
        template = self.catalog["templates"][0]
        self.assertEqual(template["ordinal"], 1)
        self.assertEqual(template["tags"][0]["index"], 0)
        self.assertEqual(template["tags"][0]["kind"], "input")
        self.assertEqual(template["tags"][0]["data_type"], "bool")

    def test_system_mapping_moves_running_and_protocol_selects_driver(self):
        self.plan['mappings'] = [{'channel': 'BitInput0', 'object_id': 'sensor', 'tag_index': 0},
                                 {'channel': 'BitOutput0', 'object_id': 'conveyor', 'tag_index': 0},
                                 {'channel': 'BitInput1', 'system_tag': 'RunInputKey'}]
        result = build_scene(self.plan, self.catalog, self.output,
                             {'name': 'modbus_tcp', 'factoryio_role': 'client', 'plc_role': 'server'})
        root = ET.parse(self.output).getroot()
        self.assertEqual(root.find('Drivers').get('CurrentDriver'), '64')
        self.assertIsNone(root.find('Drivers/ModbusTCPClient/BitInput9'))
        self.assertEqual(result['io_contract']['channel_counts']['BitInputCount'], 2)
        self.assertEqual(self.source.read_bytes(), self.source_bytes)

    def test_build_preserves_sources_metadata_groups_and_resolved_inherited_refs(self):
        result = build_scene(self.plan, self.catalog, self.output)
        self.assertEqual(self.source.read_bytes(), self.source_bytes)
        self.assertTrue(result["validation"]["passed"])
        self.assertTrue(validate_scene(self.output)["passed"])
        root, source_root = ET.parse(self.output).getroot(), ET.parse(self.source).getroot()
        self.assertEqual(root.attrib, source_root.attrib)
        self.assertEqual(root.find("Camera/CameraPosition").attrib,
                         source_root.find("Camera/CameraPosition").attrib)
        self.assertEqual(root.find("Screenshot").attrib, {"Data": ""})
        self.assertNotIn("stale-thumbnail-content", self.output.read_text(encoding="utf-8-sig"))
        manifest = result["manifest"]
        self.assertFalse(manifest["screenshot_removed"])
        self.assertTrue(manifest["source_thumbnail_removed"])
        self.assertTrue(manifest["thumbnail_placeholder"])
        self.assertFalse(manifest["software_load_confirmed"])
        self.assertFalse(result["io_contract"]["transport_confirmed"])
        groups = [obj["group_key"] for obj in manifest["objects"]]
        self.assertEqual(groups[0], groups[1])
        self.assertNotEqual(groups[0], source_root.find("Object").get("GroupKey"))
        contract = result["io_contract"]
        tag_keys = {tag["key"] for tag in contract["tags"]}
        self.assertEqual(len(contract["mappings"]), 3)
        for node in root.find("Drivers").iter():
            if node.get("PointIOKey"):
                self.assertIn(node.get("PointIOKey"), tag_keys)
        self.assertEqual(contract["mappings"][0]["offset"], 12)
        self.assertEqual(contract["channel_offsets"]["BitInputOffset"], 12)
        self.assertEqual(contract["driver_properties"]["ReadCoils"], "False")
        self.assertEqual(root.find("Drivers").get("RunInputKey"), source_root.find("Drivers").get("RunInputKey"))

    def test_native_change_check_envelope_survives_sources_without_metadata(self):
        source_root = ET.parse(self.source).getroot()
        for name in ("Screenshot", "Camera", "Description"):
            for node in source_root.findall(name):
                source_root.remove(node)
        ET.ElementTree(source_root).write(self.source, encoding="utf-8")
        catalog = build_catalog([self.source])
        plan = {"scene_id": "without-metadata", "base_source_id": catalog["sources"][0]["id"],
                "objects": [{"id": "sensor", "template_id": catalog["templates"][0]["template_id"]}]}
        build_scene(plan, catalog, self.output)
        root = ET.parse(self.output).getroot()
        # These are required by the actual native change checker, even though
        # the first loader tolerates absent Screenshot data.
        for name in ("Screenshot", "Camera", "Description"):
            self.assertEqual(len(root.findall(name)), 1)
        self.assertEqual(root.find("Screenshot").get("Data"), "")

    def test_revision_preserves_uuid_and_updates_consistent_yaw(self):
        before = build_scene(self.plan, self.catalog, self.output)
        revised = patch_plan(self.plan, [{"object_id": "sensor", "translate": [3, 1, -5],
                                         "yaw_deg": 90, "settings": [{"path": "Sensor", "attribute": "Range", "value": 1.2}]}])
        after = build_scene(revised, self.catalog, self.output)
        self.assertEqual(self.plan["objects"][0]["position"], [10, 5, 20])
        self.assertEqual(before["manifest"]["objects"][0]["key"], after["manifest"]["objects"][0]["key"])
        self.assertEqual(before["manifest"]["objects"][0]["tags"][0]["key"], after["manifest"]["objects"][0]["tags"][0]["key"])
        self.assertNotEqual(before["manifest"]["scene_sha256"], after["manifest"]["scene_sha256"])
        self.assertEqual(after["manifest"]["objects"][0]["position"], [13, 6, 15])
        proxy = ET.parse(self.output).getroot().find("Object/Proxy")
        self.assertAlmostEqual(float(proxy.find("Rotation").get("Y")), math.sqrt(0.5))
        self.assertEqual([float(proxy.find("Forward").get(a)) for a in "XYZ"], [1, 0, 0])
        self.assertEqual([float(proxy.find("Right").get(a)) for a in "XYZ"], [0, 0, -1])
        self.assertTrue(validate_scene(self.output)["passed"])

    def test_native_integer_vectors_reject_diagonal_yaw_and_fractional_position(self):
        for update in ({'yaw_deg': 45}, {'position': [10.5, 5, 20]}):
            plan = deepcopy(self.plan); plan['objects'][0].update(update)
            with self.assertRaises(SceneError): build_scene(plan, self.catalog, self.output)
        build_scene(self.plan, self.catalog, self.output)
        tree = ET.parse(self.output); tree.getroot().find('Object/Proxy/Forward').set('X', '0.707106781')
        tree.write(self.output)
        self.assertFalse(validate_scene(self.output)['passed'])

    def test_cloned_sensor_guids_have_independent_native_memory_addresses(self):
        self.plan['objects'].append({'id': 'second_sensor', 'template_id': self.catalog['templates'][0]['template_id']})
        result = build_scene(self.plan, self.catalog, self.output)
        sensors = [o for o in result['manifest']['objects'] if o['prefab']=='DiffusePhotoelectricSensor']
        self.assertEqual(len({o['tags'][0]['address'] for o in sensors}), 2)
        self.assertEqual(len({o['tags'][0]['key'] for o in sensors}), 2)
        self.assertEqual(len(result['manifest']['native_address_reallocation']), 1)
        self.assertEqual(self.source.read_bytes(), self.source_bytes)
        mapped_keys = {m['tag_key'] for m in result['io_contract']['mappings']}
        self.assertTrue(all(m['tag_key'] in {t['key'] for t in result['io_contract']['tags']} for m in result['io_contract']['mappings']))

    def test_same_plan_produces_identical_scene_bytes_and_keys(self):
        first = build_scene(self.plan, self.catalog, self.output)
        content = self.output.read_bytes()
        second = build_scene(deepcopy(self.plan), deepcopy(self.catalog), self.output)
        self.assertEqual(self.output.read_bytes(), content)
        self.assertEqual(first, second)

    def test_duplicate_clones_have_new_keys_without_ambiguous_inheritance(self):
        duplicate = deepcopy(self.plan["objects"][0])
        duplicate["id"] = "second-sensor"
        self.plan["objects"] = [self.plan["objects"][0], duplicate]
        result = build_scene(self.plan, self.catalog, self.output)
        objects = result["manifest"]["objects"]
        self.assertNotEqual(objects[0]["key"], objects[1]["key"])
        self.assertNotEqual(objects[0]["tags"][0]["key"], objects[1]["tags"][0]["key"])
        self.assertEqual(len(result["io_contract"]["mappings"]), 1)  # only system Run
        self.assertTrue(result["manifest"]["ambiguous_inherited_mappings"])
        indices = [o.find("ComponentIO").get("Index") for o in ET.parse(self.output).getroot().findall("Object")]
        self.assertEqual(len(set(indices)), 2)
        self.plan["mappings"] = [{"channel": "BitInput0", "object_id": "sensor", "tag_index": 0},
                                 {"channel": "BitInput1", "object_id": "second-sensor", "tag_index": 0}]
        explicit = build_scene(self.plan, self.catalog, self.output)
        self.assertEqual(len(explicit["io_contract"]["mappings"]), 3)
        root = ET.parse(self.output).getroot()
        self.assertEqual(root.find("Drivers/ModbusTCPClient/PointIOCount").get("BitOutputCount"), "0")
        self.assertEqual(root.find("Drivers/ModbusTCPClient/Properties").get("ScaleFactor"), "100")
        self.assertEqual(root.find("Drivers/ModbusTCPClient/PointIOOffset").get("BitInputOffset"), "12")

    def test_explicit_mapping_can_reuse_a_system_slot_and_rebuilds_counts(self):
        self.plan["mappings"] = [{"channel": "BitInput9", "object_id": "sensor", "tag_index": 0}]
        result = build_scene(self.plan, self.catalog, self.output)
        self.assertEqual(len(result["io_contract"]["mappings"]), 1)
        self.assertEqual(result["io_contract"]["mappings"][0]["tag_id"], "sensor/tag/0")
        self.assertEqual(len(result["manifest"]["replaced_system_mappings"]), 1)
        self.assertIn("00000000-0000-0000-0000-000000000090", result["io_contract"]["unmapped_tag_keys"])

    def test_mixed_sources_preserve_source_group_boundaries(self):
        other = self.directory / "other.factoryio"
        other.write_text(FIXTURE.replace('Text="fixture"', 'Text="other"'), encoding="utf-8")
        catalog = build_catalog([self.source, other])
        plan = deepcopy(self.plan)
        plan["objects"] = [plan["objects"][0], {"id": "foreign-sensor", "template_id": catalog["templates"][2]["template_id"]}]
        result = build_scene(plan, catalog, self.output)
        self.assertNotEqual(*[obj["group_key"] for obj in result["manifest"]["objects"]])
        self.assertEqual(result["io_contract"]["mappings"][0]["tag_id"], "sensor/tag/0")

    def test_settings_and_tag_names_edit_existing_safe_fields(self):
        self.plan["objects"][0]["tag_names"] = {"0": "Entry & check"}
        self.plan["objects"][0]["settings"] = [
            {"path": "Sensor", "attribute": "Range", "value": 2},
            {"path": "OperatingMode/GroupIO/BinaryInput", "attribute": "UseForcedValue", "value": True}]
        result = build_scene(self.plan, self.catalog, self.output)
        self.assertEqual(result["manifest"]["objects"][0]["tags"][0]["name"], "Entry & check")
        self.assertTrue(result["manifest"]["objects"][0]["tags"][0]["use_forced_value"])
        self.assertEqual(ET.parse(self.output).getroot().find("Object/Sensor").get("Range"), "2")

    def test_reject_unsafe_and_nonexistent_settings_without_writing(self):
        for path, attribute, value in [("Proxy", "Key", "bad"), ("ComponentIO", "CurrentOperatingMode", 3),
                                       ("../Drivers", "Range", 1), ("Sensor[2]", "Range", 1),
                                       ("Sensor/NewNode", "Range", 1), ("Sensor", "Missing", 1),
                                       ("Proxy/Position", "X", 1), ("Sensor", "Range", float("nan")),
                                       ("Sensor", "Range", True), ("Sensor", "Range", -1)]:
            with self.subTest(path=path, attribute=attribute):
                plan = deepcopy(self.plan)
                plan["objects"][0]["settings"] = [{"path": path, "attribute": attribute, "value": value}]
                with self.assertRaises(SceneError):
                    build_scene(plan, self.catalog, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), self.source_bytes)

    def test_mapping_mismatch_unknown_targets_and_collisions_fail(self):
        cases = [[{"channel": "BitOutput0", "object_id": "sensor", "tag_index": 0}],
                 [{"channel": "FloatInput0", "object_id": "sensor", "tag_index": 0}],
                 [{"channel": "BitInput0", "object_id": "unknown", "tag_index": 0}],
                 [{"channel": "BitInput0", "object_id": "sensor", "tag_index": 1}],
                 [{"channel": "BitInput0", "object_id": "sensor", "tag_index": 0},
                  {"channel": "BitInput1", "object_id": "sensor", "tag_index": 0}],
                 [{"channel": "BitInput0", "object_id": "sensor", "tag_index": True}]]
        for mappings in cases:
            with self.subTest(mappings=mappings):
                with self.assertRaises(SceneError):
                    build_scene({**self.plan, "mappings": mappings}, self.catalog, self.output)
        self.assertFalse(self.output.exists())

    def test_invalid_patch_targets_vectors_and_operations_fail(self):
        cases = [{"object_id": "unknown", "position": [1, 2, 3]},
                 {"object_id": "sensor", "position": [1, 2, float("inf")]},
                 {"object_id": "sensor", "yaw_deg": float("nan")},
                 {"object_id": "sensor", "position": [1, 2]},
                 {"object_id": "sensor", "position": [1, 2, 3], "translate": [1, 2, 3]},
                 {"object_id": "conveyor", "translate": [1, 2, 3]},
                 {"object_id": "sensor", "template_id": "replace"}]
        for patch in cases:
            with self.subTest(patch=patch):
                with self.assertRaises(SceneError):
                    patch_plan(self.plan, [patch])

    def test_nonfinite_positions_and_duplicate_ids_fail_build(self):
        for position in [[float("inf"), 0, 0], [True, 0, 0], [1, 2], [10**1000, 0, 0]]:
            plan = deepcopy(self.plan)
            plan["objects"][0]["position"] = position
            with self.assertRaises(SceneError):
                build_scene(plan, self.catalog, self.output)
        plan = deepcopy(self.plan)
        plan["objects"].append(deepcopy(plan["objects"][0]))
        with self.assertRaises(SceneError):
            build_scene(plan, self.catalog, self.output)

    def test_overwrite_source_and_stale_catalog_rejected(self):
        with self.assertRaises(SceneError):
            build_scene(self.plan, self.catalog, self.source)
        self.assertEqual(self.source.read_bytes(), self.source_bytes)
        self.source.write_bytes(self.source_bytes + b"\n")
        with self.assertRaisesRegex(SceneError, "hash changed"):
            build_scene(self.plan, self.catalog, self.output)
        self.assertFalse(self.output.exists())

    def test_structural_validator_detects_duplicates_and_dangling_references(self):
        root = ET.fromstring(FIXTURE)
        root.append(deepcopy(root.find("Object")))
        root.find("Drivers/ModbusTCPClient/BitOutput0").set("PointIOKey", "missing")
        bad = self.directory / "invalid.factoryio"
        ET.ElementTree(root).write(bad, encoding="utf-8")
        result = validate_scene(bad)
        self.assertFalse(result["passed"])
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("duplicate_object_key", codes)
        self.assertIn("duplicate_io_key", codes)
        self.assertIn("driver_reference", codes)
        with self.assertRaises(SceneError):
            build_catalog([bad])

    def test_unknown_root_and_dtd_rejected(self):
        bad = self.directory / "bad.factoryio"
        for xml in ["<Unknown/>", '<!DOCTYPE FactoryIO [<!ENTITY x "value">]><FactoryIO/>']:
            bad.write_text(xml, encoding="utf-8")
            self.assertFalse(validate_scene(bad)["passed"])
            with self.assertRaises(SceneError):
                build_catalog([bad])

    def test_structural_validator_rejects_transform_and_duplicate_driver_errors(self):
        root = ET.fromstring(FIXTURE)
        root.find("Object/Proxy/Rotation").set("Y", "0.707106781")
        root.find("Object/Proxy/Rotation").set("W", "0.707106781")
        root.find("Drivers").append(deepcopy(root.find("Drivers/ModbusTCPClient")))
        bad = self.directory / "invalid.factoryio"
        ET.ElementTree(root).write(bad, encoding="utf-8")
        result = validate_scene(bad)
        self.assertFalse(result["passed"])
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("basis", codes)
        self.assertIn("duplicate_driver", codes)

    def test_source_hardlink_output_is_protected(self):
        import os
        alias = self.directory / "source-alias.factoryio"
        os.link(self.source, alias)
        with self.assertRaisesRegex(SceneError, "overwrite"):
            build_scene(self.plan, self.catalog, alias)
        self.assertEqual(alias.read_bytes(), self.source_bytes)

    def test_failed_revision_does_not_replace_existing_output(self):
        build_scene(self.plan, self.catalog, self.output)
        before = self.output.read_bytes()
        plan = deepcopy(self.plan)
        plan["objects"][0]["yaw_degree"] = 90
        with self.assertRaisesRegex(SceneError, "Unknown planned object"):
            build_scene(plan, self.catalog, self.output)
        self.assertEqual(self.output.read_bytes(), before)

    def test_duplicate_singleton_driver_configuration_is_rejected(self):
        for name in ("PointIOCount", "PointIOOffset", "Properties"):
            with self.subTest(configuration=name):
                root = ET.fromstring(FIXTURE)
                driver = root.find("Drivers/ModbusTCPClient")
                second = deepcopy(driver.find(name))
                if name == "PointIOOffset":
                    second.set("BitInputOffset", "200")
                driver.append(second)
                bad = self.directory / "duplicate.factoryio"
                ET.ElementTree(root).write(bad, encoding="utf-8")
                validation = validate_scene(bad)
                self.assertFalse(validation["passed"])
                self.assertIn("duplicate_driver_configuration", {i["code"] for i in validation["issues"]})
                with self.assertRaises(SceneError):
                    build_catalog([bad])
                # A caller cannot bypass structural checks by supplying matching hashes.
                catalog = deepcopy(self.catalog)
                catalog["sources"][0]["path"] = str(bad)
                catalog["sources"][0]["sha256"] = hashlib.sha256(bad.read_bytes()).hexdigest()
                with self.assertRaisesRegex(SceneError, "invalid structure"):
                    build_scene(self.plan, catalog, self.output)
                self.assertFalse(self.output.exists())

    def test_known_scalar_field_syntax_and_ranges_are_validated(self):
        tag_path = "Object/OperatingMode/GroupIO/BinaryInput"
        cases = [(tag_path, "Address", ["garbage", "-1", "1.5", "1_000", "+1", "2147483648"]),
                 (tag_path, "OpenCircuit", ["garbage", "0"]),
                 (tag_path, "ShortCircuit", ["garbage", "1"]),
                 (tag_path, "UseForcedValue", ["garbage", "false"]),
                 (tag_path, "ForcedValue", ["garbage", "0", "NaN"]),
                 ("Object/ComponentIO", "Index", ["garbage", "-2", "1.5", "2147483648"]),
                 ("Object/ComponentIO", "CurrentOperatingMode", ["garbage", "-1", "1.5"]),
                 ("Object/Sensor", "Range", ["garbage", "NaN", "1e999", "-0.1", "1_000"]),
                 ("Object[2]/Encoder", "Enabled", ["garbage"]),
                 ("Drivers/ModbusTCPClient/Properties", "ReadCoils", ["garbage"]),
                 ("Drivers/ModbusTCPClient/Properties", "ReadHoldingRegisters", ["garbage"]),
                 ("Drivers/ModbusTCPClient/Properties", "ScaleFactor", ["0", "NaN", "1e999"])]
        for path, field, values in cases:
            for value in values:
                with self.subTest(path=path, field=field, value=value):
                    root = ET.fromstring(FIXTURE)
                    root.find(path).set(field, value)
                    bad = self.directory / "bad-scalar.factoryio"
                    ET.ElementTree(root).write(bad, encoding="utf-8")
                    validation = validate_scene(bad)
                    self.assertFalse(validation["passed"])
                    self.assertIn("scalar", {i["code"] for i in validation["issues"]})
                    with self.assertRaises(SceneError):
                        build_catalog([bad])

    def test_static_index_and_nonnegative_operating_mode_are_not_guessed_enums(self):
        root = ET.fromstring(FIXTURE)
        root.find("Object/ComponentIO").set("Index", "-1")
        root.find("Object/ComponentIO").set("CurrentOperatingMode", "999")
        other = self.directory / "scalar-syntax-valid.factoryio"
        ET.ElementTree(root).write(other, encoding="utf-8")
        result = validate_scene(other)
        self.assertTrue(result["passed"])
        self.assertFalse(result["software_load_confirmed"])

    def _numeric_fixture(self, io_type):
        root = ET.fromstring(FIXTURE)
        tag = root.find("Object/OperatingMode/GroupIO/BinaryInput")
        tag.tag = io_type
        tag.set("ForcedValue", "0")
        for driver in root.find("Drivers"):
            node = driver.find("BitInput0")
            node.tag = "NumericInput0"
            driver.find("PointIOCount").set("NumericInputCount", "1")
            driver.find("PointIOOffset").set("NumericInputOffset", "0")
        return root

    def test_forced_values_must_match_numeric_io_types(self):
        for io_type, values in [("IntInput", ["1.5", "True", "garbage", "2147483648", "-2147483649"]),
                                ("AnalogueInput", ["True", "NaN", "1e999", "garbage"])]:
            for value in values:
                with self.subTest(io_type=io_type, value=value):
                    root = self._numeric_fixture(io_type)
                    root.find("Object/OperatingMode/GroupIO/" + io_type).set("ForcedValue", value)
                    bad = self.directory / "bad-forced.factoryio"
                    ET.ElementTree(root).write(bad, encoding="utf-8")
                    validation = validate_scene(bad)
                    self.assertFalse(validation["passed"])
                    self.assertTrue(any(i["code"] == "scalar" and i.get("attribute") == "ForcedValue"
                                        for i in validation["issues"]))

    def test_known_configuration_numbers_and_booleans_are_checked(self):
        for field, value in [("MinTimeToEmit", "garbage"), ("MaxTimeToEmit", "NaN"),
                             ("UpTo", "-1"), ("RandomPartPosition", "garbage"),
                             ("RandomPartOrientation", "1")]:
            with self.subTest(field=field):
                root = ET.fromstring(FIXTURE)
                emitter = ET.SubElement(root.find("Object"), "Emitter", {
                    "MinTimeToEmit": "1", "MaxTimeToEmit": "2", "UpTo": "0",
                    "RandomPartPosition": "False", "RandomPartOrientation": "False"})
                emitter.set(field, value)
                bad = self.directory / "bad-emitter.factoryio"
                ET.ElementTree(root).write(bad, encoding="utf-8")
                self.assertFalse(validate_scene(bad)["passed"])
        root = ET.fromstring(FIXTURE)
        root.find("Object/Proxy/FineRotation").text = "NaN"
        bad = self.directory / "bad-fine.factoryio"
        ET.ElementTree(root).write(bad, encoding="utf-8")
        self.assertFalse(validate_scene(bad)["passed"])

    def test_documented_modbus_bit_channel_limit_and_huge_index_rejection(self):
        for channel in ("BitInput256", "BitInput1000000000000000000000000000"):
            with self.subTest(channel=channel):
                plan = {**self.plan, "mappings": [{"channel": channel, "object_id": "sensor", "tag_index": 0}]}
                with self.assertRaises(SceneError):
                    build_scene(plan, self.catalog, self.output)
                self.assertFalse(self.output.exists())
        plan = {**self.plan, "mappings": [{"channel": "BitInput255", "object_id": "sensor", "tag_index": 0}]}
        result = build_scene(plan, self.catalog, self.output)
        self.assertEqual(result["io_contract"]["channel_counts"]["BitInputCount"], 256)
        self.assertEqual(result["validation"]["supported_limits"]["Bit_channel_count"], 256)

    def test_documented_numeric_channel_limit_and_boundary(self):
        root = self._numeric_fixture("AnalogueInput")
        numeric = self.directory / "numeric.factoryio"
        ET.ElementTree(root).write(numeric, encoding="utf-8")
        catalog = build_catalog([numeric])
        plan = {"scene_id": "numeric-limit", "base_source_id": catalog["sources"][0]["id"],
                "objects": [{"id": "sensor", "template_id": catalog["templates"][0]["template_id"]}],
                "mappings": [{"channel": "NumericInput63", "object_id": "sensor", "tag_index": 0}]}
        result = build_scene(plan, catalog, self.output)
        self.assertEqual(result["io_contract"]["channel_counts"]["NumericInputCount"], 64)
        self.assertFalse(result["io_contract"]["supported_limits"]["register_width_confirmed"])
        before = self.output.read_bytes()
        plan["mappings"][0]["channel"] = "NumericInput64"
        with self.assertRaises(SceneError):
            build_scene(plan, catalog, self.output)
        self.assertEqual(self.output.read_bytes(), before)

    def test_source_modbus_counts_offsets_and_slot_spans_are_bounded(self):
        cases = [("PointIOCount", "BitInputCount", "257"),
                 ("PointIOCount", "NumericInputCount", "65"),
                 ("PointIOCount", "FloatInputCount", "65"),
                 ("PointIOCount", "IntOutputCount", "65"),
                 ("PointIOCount", "BitInputCount", "1000000000000000000000000000"),
                 ("PointIOOffset", "BitInputOffset", "65536"),
                 ("PointIOOffset", "BitInputOffset", "65530")]
        for element, field, value in cases:
            with self.subTest(field=field, value=value):
                root = ET.fromstring(FIXTURE)
                root.find("Drivers/ModbusTCPClient/" + element).set(field, value)
                bad = self.directory / "bad-modbus.factoryio"
                ET.ElementTree(root).write(bad, encoding="utf-8")
                self.assertFalse(validate_scene(bad)["passed"])
                with self.assertRaises(SceneError):
                    build_catalog([bad])
        root = ET.fromstring(FIXTURE)
        root.find("Drivers/ModbusTCPClient/PointIOOffset").set("BitInputOffset", "65526")
        boundary = self.directory / "boundary.factoryio"
        ET.ElementTree(root).write(boundary, encoding="utf-8")
        self.assertTrue(validate_scene(boundary)["passed"])
        self.assertEqual(validate_scene(boundary)["supported_limits"]["address_limit_basis"], "implementation_policy")
        catalog = build_catalog([boundary])
        plan = {"scene_id": "address-overflow", "base_source_id": catalog["sources"][0]["id"],
                "objects": [{"id": "sensor", "template_id": catalog["templates"][0]["template_id"]}],
                "mappings": [{"channel": "BitInput10", "object_id": "sensor", "tag_index": 0}]}
        with self.assertRaisesRegex(SceneError, "failed structural validation"):
            build_scene(plan, catalog, self.output)
        self.assertFalse(self.output.exists())


class RealSourceTests(unittest.TestCase):
    def test_real_sources_full_clone_and_source_preservation(self):
        workspace = Path(__file__).resolve().parents[1]
        paths = [Path(r"E:\Program Files (x86)\factory\Scenes\Sorting by Weight.factoryio"),
                 workspace / "scenes" / "Sorting by Height (Learn).factoryio"]
        if not all(path.is_file() for path in paths):
            self.skipTest("Factory I/O reference scenes are not installed on this host")
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        catalog = build_catalog(paths)
        with tempfile.TemporaryDirectory() as folder:
            for source in catalog["sources"]:
                templates = [t for t in catalog["templates"] if t["source_id"] == source["id"]]
                plan = {"scene_id": "real-" + source["id"], "base_source_id": source["id"],
                        "objects": [{"id": f"object-{t['ordinal']}", "template_id": t["template_id"]}
                                    for t in templates]}
                result = build_scene(plan, catalog, Path(folder) / (source["id"] + ".factoryio"))
                self.assertTrue(result["validation"]["passed"])
                self.assertEqual(len(result["manifest"]["objects"]), source["object_count"])
                original = ET.parse(source["path"]).getroot()
                original_modbus_mappings = [n for n in original.find("Drivers/ModbusTCPClient") if n.get("PointIOKey")]
                self.assertEqual(len(result["io_contract"]["mappings"]), len(original_modbus_mappings))
                self.assertFalse(result["manifest"]["removed_driver_mappings"])
                self.assertTrue(validate_scene(Path(result["manifest"]["scene_path"]))["passed"])
        after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
