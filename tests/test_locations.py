import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class LocationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine()
        )
        self.actor = Actor("admin", "admin")
        self.participant = self.service.create(
            self.actor, "participant", {"name": "Participant One"}
        )
        self.consent = self.service.create(
            self.actor,
            "consent",
            {"participant_id": self.participant["id"], "scope": ["research"]},
        )
        self.consent = self.service.transition(
            self.actor,
            self.consent["id"],
            "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _create_sample(self, code):
        return self.service.create(
            self.actor,
            "sample",
            {
                "participant_id": self.participant["id"],
                "sample_code": code,
                "collected_at": "2026-01-01",
            },
        )

    def _store(self, sample, freezer, position):
        return self.service.transition(
            self.actor,
            sample["id"],
            "store",
            {"freezer": freezer, "position": position, "consent_id": self.consent["id"]},
        )

    def test_store_rejects_occupied_slot(self):
        first = self._create_sample("B-001")
        second = self._create_sample("B-002")
        self._store(first, "F1", "A1")
        with self.assertRaises(ConflictError) as ctx:
            self._store(second, "F1", "A1")
        self.assertIn("已被在库样本", str(ctx.exception))
        self.assertIn(first["id"], str(ctx.exception))
        # 第二个样本仍是 collected，未被错误置为 stored
        self.assertEqual(self.service.get(second["id"])["status"], "collected")

    def test_relocate_moves_history_and_releases_old_slot(self):
        sample = self._create_sample("B-001")
        self._store(sample, "F1", "A1")
        moved = self.service.transition(
            self.actor,
            sample["id"],
            "relocate",
            {"freezer": "F2", "position": "C3"},
        )
        self.assertEqual(moved["status"], "stored")
        self.assertEqual(moved["data"]["freezer"], "F2")
        self.assertEqual(moved["data"]["position"], "C3")
        self.assertEqual(moved["version"], 3)

        history = self.service.location_history(sample["id"])
        self.assertEqual(len(history), 2)
        old, new = history
        self.assertEqual((old["freezer"], old["position"]), ("F1", "A1"))
        self.assertIsNotNone(old["released_at"])
        self.assertFalse(old["active"])
        self.assertEqual((new["freezer"], new["position"]), ("F2", "C3"))
        self.assertTrue(new["active"])
        self.assertEqual(old["released_at"], new["occupied_at"])

        # 旧位已释放，可以再存样本
        other = self._create_sample("B-002")
        self._store(other, "F1", "A1")
        locations = {(item["freezer"], item["position"]): item["sample_id"]
                     for item in self.service.list_locations()}
        self.assertEqual(len(locations), 2)
        self.assertEqual(locations[("F1", "A1")], other["id"])
        self.assertEqual(locations[("F2", "C3")], sample["id"])

    def test_relocate_conflict_keeps_original_position(self):
        first = self._create_sample("B-001")
        second = self._create_sample("B-002")
        self._store(first, "F1", "A1")
        self._store(second, "F1", "B2")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                self.actor, second["id"], "relocate", {"freezer": "F1", "position": "A1"}
            )
        message = str(ctx.exception)
        self.assertIn(first["id"], message)
        self.assertIn("保留在原位置", message)
        self.assertIn("F1/B2", message)

        untouched = self.service.get(second["id"])
        self.assertEqual(untouched["data"]["freezer"], "F1")
        self.assertEqual(untouched["data"]["position"], "B2")
        # 冲突不产生新的库位记录
        self.assertEqual(len(self.service.location_history(second["id"])), 1)

    def test_relocate_to_same_slot_rejected(self):
        sample = self._create_sample("B-001")
        self._store(sample, "F1", "A1")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor, sample["id"], "relocate", {"freezer": "F1", "position": "A1"}
            )

    def test_destroy_releases_slot(self):
        sample = self._create_sample("B-001")
        self._store(sample, "F1", "A1")
        self.service.transition(
            self.actor, sample["id"], "destroy", {"reason": "expired"}
        )
        other = self._create_sample("B-002")
        self._store(other, "F1", "A1")
        locations = self.service.list_locations()
        self.assertEqual(len(locations), 1)
        self.assertEqual(locations[0]["sample_id"], other["id"])
        # 被销毁样本的历史仍保留占用与释放时间
        history = self.service.location_history(sample["id"])
        self.assertEqual(len(history), 1)
        self.assertIsNotNone(history[0]["released_at"])

    def test_audit_records_before_and_after_location(self):
        sample = self._create_sample("B-001")
        self._store(sample, "F1", "A1")
        self.service.transition(
            self.actor, sample["id"], "relocate", {"freezer": "F2", "position": "C3"}
        )
        relocate_audit = [
            item for item in self.service.audit_log(sample["id"])
            if item["action"] == "relocate"
        ][0]
        move = relocate_audit["detail"]["location"]
        self.assertEqual(move["from"]["freezer"], "F1")
        self.assertEqual(move["from"]["position"], "A1")
        self.assertIn("released_at", move["from"])
        self.assertEqual(move["to"]["freezer"], "F2")
        self.assertEqual(move["to"]["position"], "C3")
        self.assertIn("occupied_at", move["to"])

    def test_history_unknown_sample_raises_not_found(self):
        from src.domain import NotFoundError
        with self.assertRaises(NotFoundError):
            self.service.location_history("missing")


if __name__ == "__main__":
    unittest.main()
