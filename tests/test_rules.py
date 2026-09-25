import unittest


from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        participant = self.rules.validate_create(self.admin, "participants", {"name": "Participant"})
        self.assertEqual(participant["name"], "Participant")
        with self.assertRaises(ValidationError):
            self.rules.validate_create(self.admin, "participants", {"name": ""})

    def test_relocate_is_a_stored_to_stored_transition(self):
        allowed, target = self.rules.TRANSITIONS["sample"]["relocate"]
        self.assertEqual(allowed, ("stored",))
        self.assertEqual(target, "stored")
        self.assertEqual(
            self.rules.ACTION_REQUIRED[("sample", "relocate")], ("freezer", "position")
        )
        self.assertEqual(
            self.rules.location_effect("sample", "relocate"), "relocate"
        )
        self.assertNotIn(
            "viewer", self.rules.ROLE_ACTIONS["relocate"]
        )

    def test_viewer_cannot_relocate(self):
        stored = {"id": "s1", "kind": "sample", "status": "stored", "data": {}}
        with self.assertRaises(PermissionDenied):
            self.rules.validate_transition(
                Actor("v", "viewer"),
                stored,
                "relocate",
                {"freezer": "F1", "position": "A1"},
            )


if __name__ == "__main__":
    unittest.main()
