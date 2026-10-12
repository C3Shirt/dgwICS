"""Check misleading live snapshots are rejected, not a simulated live pass."""
import copy
import importlib.util
from pathlib import Path
import sys
import unittest

folder = Path(__file__).resolve().parents[1] / "tools/factoryio_bridge"
sys.path.insert(0, str(folder))
spec = importlib.util.spec_from_file_location("bridge_verification", folder / "verify_live.py")
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)


class ObjectEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.expected = [{"key": "A", "prefab": "Belt", "position": [1, 2, 3],
                          "rotation": [0, 0.7071068, 0, -0.7071068],
                          "fine_position": 0, "fine_rotation": 0}]
        self.actual = [{"key": "A", "prefab": "Belt", "position_grid": [1, 2, 3],
                        "rotation": [0, -0.7071068, 0, 0.7071068],
                        "fine_position": 0, "fine_rotation": 0}]

    def test_equivalent_quaternion_sign_is_accepted(self):
        self.assertTrue(verification.compare_objects(self.expected, self.actual)["passed"])

    def test_equal_count_and_names_cannot_hide_another_scene(self):
        self.actual[0]["key"] = "B"
        self.assertFalse(verification.compare_objects(self.expected, self.actual)["passed"])

    def test_equal_grid_cannot_hide_fine_position_error(self):
        self.actual[0]["fine_position"] = 0.02
        self.assertFalse(verification.compare_objects(self.expected, self.actual)["passed"])

    def test_duplicate_keys_cannot_hide_object_loss(self):
        self.actual.append(copy.deepcopy(self.actual[0]))
        self.assertFalse(verification.compare_objects(self.expected, self.actual)["passed"])

    def test_invalid_rotation_cannot_pass(self):
        self.actual[0]["rotation"][0] = float("nan")
        self.assertFalse(verification.compare_objects(self.expected, self.actual)["passed"])


if __name__ == "__main__":
    unittest.main()
