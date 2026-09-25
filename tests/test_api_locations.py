import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class LocationApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repository = SQLiteRepository(Path(self.tmp.name) / "test.db")
        service = DomainService(repository, RuleEngine())
        static_dir = str(Path(__file__).resolve().parent.parent / "static")
        self.server = create_server("127.0.0.1", 0, service, RuleEngine(), static_dir)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.service = service

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def _url(self, path):
        return "http://127.0.0.1:%s%s" % (self.port, path)

    def _request(self, method, path, payload=None):
        headers = {"X-User-Id": "admin", "X-Role": "admin",
                   "Content-Type": "application/json"}
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self._url(path), data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _seed_stored_sample(self, code, freezer, position):
        _, participant = self._request("POST", "/api/participants", {"name": "Participant"})
        _, consent = self._request(
            "POST",
            "/api/consents",
            {"participant_id": participant["id"], "scope": ["research"]},
        )
        self._request(
            "POST",
            "/api/entities/%s/actions" % consent["id"],
            {"action": "activate",
             "data": {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"}},
        )
        _, sample = self._request(
            "POST",
            "/api/samples",
            {"participant_id": participant["id"], "sample_code": code,
             "collected_at": "2026-01-01"},
        )
        status, body = self._request(
            "POST",
            "/api/entities/%s/actions" % sample["id"],
            {"action": "store",
             "data": {"freezer": freezer, "position": position,
                      "consent_id": consent["id"]}},
        )
        self.assertEqual(status, 200, body)
        return sample["id"]

    def test_locations_list_and_history_endpoints(self):
        sample_id = self._seed_stored_sample("B-001", "F1", "A1")
        status, body = self._request("GET", "/api/locations")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 1)
        item = body["items"][0]
        self.assertEqual(item["freezer"], "F1")
        self.assertEqual(item["position"], "A1")
        self.assertEqual(item["sample_id"], sample_id)
        self.assertEqual(item["sample_code"], "B-001")
        self.assertEqual(item["sample_status"], "stored")

        self._request(
            "POST",
            "/api/entities/%s/actions" % sample_id,
            {"action": "relocate", "data": {"freezer": "F2", "position": "C3"}},
        )
        status, body = self._request(
            "GET", "/api/entities/%s/locations" % sample_id
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 2)
        old, new = body["items"]
        self.assertEqual((old["freezer"], old["position"], old["active"]),
                         ("F1", "A1", False))
        self.assertEqual((new["freezer"], new["position"], new["active"]),
                         ("F2", "C3", True))
        self.assertIsNotNone(old["released_at"])
        self.assertIsNone(new["released_at"])

    def test_relocate_conflict_returns_409(self):
        first = self._seed_stored_sample("B-001", "F1", "A1")
        second = self._seed_stored_sample("B-002", "F1", "B2")
        status, body = self._request(
            "POST",
            "/api/entities/%s/actions" % second,
            {"action": "relocate", "data": {"freezer": "F1", "position": "A1"}},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["type"], "ConflictError")
        self.assertIn(first, body["error"])
        self.assertIn("保留在原位置", body["error"])

    def test_history_for_unknown_sample_returns_404(self):
        status, body = self._request("GET", "/api/entities/missing/locations")
        self.assertEqual(status, 404)
        self.assertEqual(body["type"], "NotFoundError")


if __name__ == "__main__":
    unittest.main()
