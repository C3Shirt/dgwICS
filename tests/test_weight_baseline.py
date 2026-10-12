"""Signal semantics must follow component identity, not a reused display name."""
import unittest
import xml.etree.ElementTree as ET

from factoryio_agents.contracts import ContractError
from factoryio_agents.weight_baseline import _verify_component_semantics, configure_paced_emitter


class ComponentSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.root = ET.Element("Scene")
        self.rows = {}
        for index in range(9):
            prefab = "RetroreflectivePhotoelectricSensor" if index in (0, 4, 6, 8) else "DiffusePhotoelectricSensor"
            self.add_component("BitInput" + str(index), prefab, "$C_DefaultSensor_OM_Binary_NAME", "BinaryInput")
        sorter = ET.SubElement(self.root, "Object")
        ET.SubElement(sorter, "Proxy", PrefabName="PopUpWheelSorter")
        mode = ET.SubElement(sorter, "OperatingMode", Description="$C_PopUpWheelSorter_OM_SingleBinary_NAME")
        group = ET.SubElement(mode, "GroupIO")
        for index in (2, 4, 6):
            channel = "BitOutput" + str(index)
            ET.SubElement(group, "BinaryOutput", Key=channel, Name="same displayed name")
            self.rows[channel] = {"tag_key": channel}

    def add_component(self, channel, prefab, mode, io_type):
        obj = ET.SubElement(self.root, "Object")
        ET.SubElement(obj, "Proxy", PrefabName=prefab)
        group = ET.SubElement(ET.SubElement(obj, "OperatingMode", Description=mode), "GroupIO")
        ET.SubElement(group, io_type, Key=channel, Name="same displayed name")
        self.rows[channel] = {"tag_key": channel}
        return obj

    def test_observed_components_resolve_by_keys_despite_ambiguous_names(self):
        _verify_component_semantics(self.root, self.rows)

    def test_same_tag_name_on_replaced_exit_sensor_is_rejected(self):
        self.root.findall("Object")[4].find("Proxy").set("PrefabName", "DiffusePhotoelectricSensor")
        with self.assertRaisesRegex(ContractError, "semantics changed: BitInput4"):
            _verify_component_semantics(self.root, self.rows)

    def test_sorter_mode_with_different_control_semantics_is_rejected(self):
        self.root.findall("Object")[-1].find("OperatingMode").set("Description", "$C_PopUpWheelSorter_OM_DoubleBinary_NAME")
        with self.assertRaisesRegex(ContractError, "semantics changed: BitOutput2"):
            _verify_component_semantics(self.root, self.rows)

    def test_duplicate_io_key_cannot_silently_choose_a_component(self):
        self.add_component("BitInput4", "RetroreflectivePhotoelectricSensor", "$C_DefaultSensor_OM_Binary_NAME", "BinaryInput")
        with self.assertRaisesRegex(ContractError, "ownership is not unique"):
            _verify_component_semantics(self.root, self.rows)


class PacedEmitterTests(unittest.TestCase):
    def setUp(self):
        self.root = ET.fromstring('''<FactoryIO><Object><Proxy PrefabName="Emitter" Key="emitter-object"/>
        <OperatingMode Description="$C_Emitter_OM_Default_NAME"><GroupIO>
        <BinaryOutput Name="Emitter" Key="emit-key" UseForcedValue="True" ForcedValue="True"/>
        </GroupIO></OperatingMode><Emitter UpTo="0"/></Object>
        <Drivers><ModbusTCPClient><PointIOOffset BitOutputOffset="0"/><PointIOCount BitOutputCount="11"/>
        </ModbusTCPClient></Drivers></FactoryIO>''')
        self.tag = {"key": "emit-key", "id": "emitter/tag/0", "name": "Emitter", "kind": "output", "data_type": "bool"}
        self.contract = {"tags": [self.tag], "mappings": [], "channel_counts": {"BitOutputCount": 11},
                         "unmapped_tag_keys": ["emit-key"], "transport_confirmed": True, "software_load_confirmed": True}
        self.manifest = {"objects": [{"id": "emitter", "tags": [dict(self.tag)]}]}

    def test_emitter_becomes_unforced_single_item_output_and_invalidates_old_evidence(self):
        configure_paced_emitter(self.root, self.contract, self.manifest)
        self.assertEqual(self.root.find("Object/Emitter").get("UpTo"), "1")
        self.assertEqual(self.root.find("Drivers").get("CurrentDriver"), "64")
        self.assertEqual(self.root.find("Object/OperatingMode/GroupIO/BinaryOutput").get("UseForcedValue"), "False")
        self.assertFalse(self.tag["use_forced_value"])
        self.assertEqual(self.contract["mappings"][0]["channel"], "BitOutput11")
        self.assertEqual(self.contract["channel_counts"]["BitOutputCount"], 12)
        self.assertEqual(self.contract["unmapped_tag_keys"], [])
        self.assertFalse(self.contract["transport_confirmed"])
        self.assertFalse(self.contract["software_load_confirmed"])

    def test_existing_output_mapping_cannot_be_overwritten(self):
        self.contract["mappings"].append({"channel": "BitOutput11", "tag_key": "another-actuator"})
        before = ET.tostring(self.root)
        with self.assertRaisesRegex(ContractError, "collides"):
            configure_paced_emitter(self.root, self.contract, self.manifest)
        self.assertEqual(ET.tostring(self.root), before)

    def test_nonzero_offset_is_not_assumed_to_be_the_observed_template(self):
        self.root.find("Drivers/ModbusTCPClient/PointIOOffset").set("BitOutputOffset", "100")
        with self.assertRaisesRegex(ContractError, "zero-offset"):
            configure_paced_emitter(self.root, self.contract, self.manifest)

    def test_prepared_scene_does_not_duplicate_emitter_mapping_on_repeat(self):
        configure_paced_emitter(self.root, self.contract, self.manifest)
        configure_paced_emitter(self.root, self.contract, self.manifest)
        self.assertEqual(len(self.contract["mappings"]), 1)
        self.assertEqual(len(self.root.findall("Drivers/ModbusTCPClient/BitOutput11")), 1)


if __name__ == "__main__":
    unittest.main()
