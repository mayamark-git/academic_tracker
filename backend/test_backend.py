import unittest

from fastapi.testclient import TestClient

from main import create_app
from store import InMemoryStore, make_doc_id


class BackendApiTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.client = TestClient(create_app(self.store))

    def test_health_and_ready(self):
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/readyz").status_code, 200)

    def test_ready_reports_store_failure(self):
        class Broken(InMemoryStore):
            def ping(self):
                raise RuntimeError("down")

        client = TestClient(create_app(Broken()))
        self.assertEqual(client.get("/readyz").status_code, 503)

    def test_paper_crud_flow(self):
        created = self.client.post("/papers", json={"title": " 地域の居場所 ", "doi": "10.1/x"})
        self.assertEqual(created.status_code, 201)
        body = created.json()
        self.assertEqual(body["title"], "地域の居場所")
        self.assertEqual(body["id"], make_doc_id("地域の居場所"))
        self.assertFalse(body["is_read"])

        doc_id = body["id"]
        patched = self.client.patch(f"/papers/{doc_id}", json={"is_read": True})
        self.assertEqual(patched.status_code, 200)
        self.assertTrue(patched.json()["is_read"])
        self.assertEqual(patched.json()["doi"], "10.1/x")

        listed = self.client.get("/papers").json()
        self.assertEqual([p["id"] for p in listed], [doc_id])

        self.assertEqual(self.client.delete(f"/papers/{doc_id}").status_code, 204)
        self.assertEqual(self.client.get(f"/papers/{doc_id}").status_code, 404)

    def test_duplicate_key_conflicts(self):
        self.client.post("/researchers", json={"name": "Taro Yamada"})
        again = self.client.post("/researchers", json={"name": "taro  yamada"})
        self.assertEqual(again.status_code, 409)

    def test_blank_key_and_unknown_id_rejected(self):
        self.assertEqual(self.client.post("/keywords", json={"keyword": "  "}).status_code, 422)
        self.assertEqual(self.client.patch("/societies/missing", json={"category": "x"}).status_code, 404)
        self.assertEqual(self.client.delete("/societies/missing").status_code, 404)

    def test_patch_cannot_rename_key(self):
        created = self.client.post("/societies", json={"name": "日本社会学会"}).json()
        patched = self.client.patch(
            f"/societies/{created['id']}", json={"name": "other", "is_target": True}
        ).json()
        self.assertEqual(patched["name"], "日本社会学会")
        self.assertTrue(patched["is_target"])

    def test_list_limit_validated(self):
        self.assertEqual(self.client.get("/papers?limit=0").status_code, 422)
        self.assertEqual(self.client.get("/papers?limit=501").status_code, 422)


if __name__ == "__main__":
    unittest.main()
