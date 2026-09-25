import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ConcurrentRelocateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine()
        )
        self.actor = Actor("admin", "admin")
        participant = self.service.create(
            self.actor, "participant", {"name": "Participant One"}
        )
        consent = self.service.create(
            self.actor,
            "consent",
            {"participant_id": participant["id"], "scope": ["research"]},
        )
        consent = self.service.transition(
            self.actor,
            consent["id"],
            "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"},
        )
        self.samples = []
        for index in range(2):
            sample = self.service.create(
                self.actor,
                "sample",
                {
                    "participant_id": participant["id"],
                    "sample_code": "B-%03d" % index,
                    "collected_at": "2026-01-01",
                },
            )
            self.service.transition(
                self.actor,
                sample["id"],
                "store",
                {
                    "freezer": "F1",
                    "position": "S%d" % index,
                    "consent_id": consent["id"],
                },
            )
            self.samples.append(sample["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_requests_for_same_slot_only_one_wins(self):
        barrier = threading.Barrier(2)
        results = [None, None]

        def move(index):
            service = DomainService(
                SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine()
            )
            barrier.wait()
            try:
                service.transition(
                    Actor("admin", "admin"),
                    self.samples[index],
                    "relocate",
                    {"freezer": "F9", "position": "Z9"},
                )
                results[index] = ("ok", None)
            except ConflictError as exc:
                results[index] = ("conflict", str(exc))

        threads = [threading.Thread(target=move, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        statuses = sorted(result[0] for result in results)
        self.assertEqual(statuses, ["conflict", "ok"])

        winner = next(i for i, result in enumerate(results) if result[0] == "ok")
        loser = 1 - winner
        # 胜者落在新位
        winner_entity = self.service.get(self.samples[winner])
        self.assertEqual(winner_entity["data"]["freezer"], "F9")
        self.assertEqual(winner_entity["data"]["position"], "Z9")
        # 败者原位置保持，冲突说明指向胜者
        loser_entity = self.service.get(self.samples[loser])
        self.assertEqual(loser_entity["data"]["freezer"], "F1")
        self.assertEqual(loser_entity["data"]["position"], "S%d" % loser)
        self.assertIn("保留在原位置", results[loser][1])

        # 库位表中目标格只有一个在占记录
        target = self.service.repository.get_slot_occupant("F9", "Z9")
        self.assertIsNotNone(target)
        self.assertEqual(target["sample_id"], self.samples[winner])
        self.assertEqual(len(self.service.list_locations()), 2)


if __name__ == "__main__":
    unittest.main()
