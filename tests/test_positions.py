import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class PositionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _stored_sample(self, code, freezer, position):
        participant = self.service.create(
            self.actor, "participant", {"name": "Participant " + code}
        )
        consent = self.service.create(
            self.actor,
            "consent",
            {"participant_id": participant["id"], "scope": ["research"]},
        )
        self.service.transition(
            self.actor,
            consent["id"],
            "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"},
        )
        sample = self.service.create(
            self.actor,
            "sample",
            {
                "participant_id": participant["id"],
                "sample_code": code,
                "collected_at": "2026-01-01",
            },
        )
        return self.service.transition(
            self.actor,
            sample["id"],
            "store",
            {"freezer": freezer, "position": position, "consent_id": consent["id"]},
        )

    def _position_holder(self, freezer, position):
        row = self.repo.get_position(freezer, position)
        return row["sample_id"] if row else None

    def test_store_marks_position_occupied(self):
        sample = self._stored_sample("S-001", "F1", "A1")
        self.assertEqual(self._position_holder("F1", "A1"), sample["id"])
        self.assertEqual(
            self.repo.get_sample_position(sample["id"])["position"], "A1"
        )

    def test_store_conflict_on_occupied_position(self):
        first = self._stored_sample("S-001", "F1", "A1")
        second = self.service.create(
            self.actor,
            "sample",
            {
                "participant_id": first["data"]["participant_id"],
                "sample_code": "S-002",
                "collected_at": "2026-01-02",
            },
        )
        consent_id = first["data"]["consent_id"]
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.actor,
                second["id"],
                "store",
                {"freezer": "F1", "position": "A1", "consent_id": consent_id},
            )
        self.assertEqual(self.service.get(second["id"])["status"], "collected")
        self.assertEqual(self._position_holder("F1", "A1"), first["id"])

    def test_relocate_releases_old_and_occupies_new_with_history(self):
        sample = self._stored_sample("S-001", "F1", "A1")
        moved = self.service.transition(
            self.actor, sample["id"], "relocate", {"freezer": "F2", "position": "B3"}
        )
        self.assertEqual(moved["status"], "stored")
        self.assertEqual(moved["data"]["freezer"], "F2")
        self.assertEqual(moved["data"]["position"], "B3")
        self.assertIsNone(self._position_holder("F1", "A1"))
        self.assertEqual(self._position_holder("F2", "B3"), sample["id"])
        history = moved["data"]["position_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["from_freezer"], "F1")
        self.assertEqual(history[0]["from_position"], "A1")
        self.assertEqual(history[0]["to_freezer"], "F2")
        self.assertEqual(history[0]["to_position"], "B3")
        self.assertTrue(history[0]["changed_at"])
        audit = self.service.audit_log(entity_id=sample["id"])
        relocate_entry = [a for a in audit if a["action"] == "relocate"][0]
        self.assertEqual(
            relocate_entry["detail"]["position_change"]["from_position"], "A1"
        )
        self.assertEqual(
            relocate_entry["detail"]["position_change"]["to_position"], "B3"
        )

    def test_relocate_conflict_keeps_original_position(self):
        first = self._stored_sample("S-001", "F1", "A1")
        second = self._stored_sample("S-002", "F1", "A2")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                self.actor, second["id"], "relocate", {"freezer": "F1", "position": "A1"}
            )
        self.assertIn("keeps its current position", str(ctx.exception))
        unchanged = self.service.get(second["id"])
        self.assertEqual(unchanged["data"]["position"], "A2")
        self.assertNotIn("position_history", unchanged["data"])
        self.assertEqual(self._position_holder("F1", "A1"), first["id"])
        self.assertEqual(self._position_holder("F1", "A2"), second["id"])

    def test_concurrent_relocate_only_one_wins(self):
        first = self._stored_sample("S-001", "F1", "A1")
        second = self._stored_sample("S-002", "F1", "A2")
        outcomes = {"ok": 0, "conflict": 0}
        lock = threading.Lock()

        def move(sample_id):
            try:
                self.service.transition(
                    self.actor,
                    sample_id,
                    "relocate",
                    {"freezer": "F9", "position": "Z9"},
                )
                outcome = "ok"
            except ConflictError:
                outcome = "conflict"
            with lock:
                outcomes[outcome] += 1

        threads = [
            threading.Thread(target=move, args=(sample["id"],))
            for sample in (first, second)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(outcomes["ok"], 1)
        self.assertEqual(outcomes["conflict"], 1)
        holder = self._position_holder("F9", "Z9")
        self.assertIn(holder, (first["id"], second["id"]))
        loser = second["id"] if holder == first["id"] else first["id"]
        self.assertEqual(self.service.get(loser)["status"], "stored")
        self.assertIsNotNone(self.repo.get_sample_position(loser))

    def test_destroy_releases_position(self):
        sample = self._stored_sample("S-001", "F1", "A1")
        self.service.transition(
            self.actor, sample["id"], "destroy", {"reason": "quality failure"}
        )
        self.assertIsNone(self._position_holder("F1", "A1"))
        self.assertIsNone(self.repo.get_sample_position(sample["id"]))


if __name__ == "__main__":
    unittest.main()
