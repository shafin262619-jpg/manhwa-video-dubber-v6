"""Tests for pipeline.key_store and the /settings HTTP endpoints."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import app
from pipeline import key_store

SECRET = "AIzaSySecretKeyValue1234567890abcd"


class KeyStoreModuleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = str(Path(self._tmp.name) / "gemini_keys_store.json")

    def test_add_list_delete_happy_path(self):
        added = key_store.add_key(SECRET, label="main key", store_path=self.store)
        self.assertEqual(added["label"], "main key")
        self.assertNotIn(SECRET, added["key"])
        self.assertEqual(added["key"], "..." + SECRET[-4:])

        keys = key_store.list_keys(store_path=self.store)
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]["id"], added["id"])
        self.assertNotIn(SECRET, keys[0]["key"])

        deleted = key_store.delete_key(added["id"], store_path=self.store)
        self.assertEqual(deleted["id"], added["id"])
        self.assertEqual(key_store.list_keys(store_path=self.store), [])

    def test_add_assigns_sequential_ids(self):
        first = key_store.add_key("key-one", store_path=self.store)
        second = key_store.add_key("key-two", store_path=self.store)
        self.assertEqual(first["id"], "k1")
        self.assertEqual(second["id"], "k2")

    def test_add_empty_key_raises(self):
        with self.assertRaises(ValueError):
            key_store.add_key("   ", store_path=self.store)

    def test_delete_missing_id_raises_clear_error(self):
        with self.assertRaises(key_store.KeyNotFoundError) as ctx:
            key_store.delete_key("k999", store_path=self.store)
        self.assertIn("k999", str(ctx.exception))

    def test_delete_twice_raises(self):
        added = key_store.add_key(SECRET, store_path=self.store)
        key_store.delete_key(added["id"], store_path=self.store)
        with self.assertRaises(key_store.KeyNotFoundError):
            key_store.delete_key(added["id"], store_path=self.store)

    def test_get_active_keys_returns_raw_keys(self):
        key_store.add_key("key-alpha", store_path=self.store)
        key_store.add_key("key-beta", store_path=self.store)
        self.assertEqual(
            key_store.get_active_keys(store_path=self.store),
            ["key-alpha", "key-beta"],
        )

    def test_raw_key_never_leaks_via_module_api(self):
        key_store.add_key(SECRET, store_path=self.store)
        serialized = str(key_store.list_keys(store_path=self.store))
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn(SECRET[:-4], serialized)

    def test_add_keys_parses_newlines_and_commas(self):
        added = key_store.add_keys("AIza-key-one\nAIza-key-two,AIza-key-three", store_path=self.store)
        self.assertEqual(len(added), 3)
        self.assertEqual(added[0]["id"], "k1")
        self.assertEqual(added[1]["id"], "k2")
        self.assertEqual(added[2]["id"], "k3")
        self.assertEqual(added[0]["key"], "..." + "key-one"[-4:])
        self.assertNotIn("AIza-key-one", added[0]["key"])

    def test_add_keys_masks_all_and_keeps_active_keys(self):
        key_a = "AIzaSyAAAAAAAAAAAAAAAAAAAA111111"
        key_b = "AIzaSyBBBBBBBBBBBBBBBBBBBB222222"
        key_c = "AIzaSyCCCCCCCCCCCCCCCCCCCC333333"
        key_store.add_keys(f"{key_a}\n{key_b}\n{key_c}", store_path=self.store)
        serialized = str(key_store.list_keys(store_path=self.store))
        for raw in (key_a, key_b, key_c):
            self.assertNotIn(raw, serialized)
        self.assertEqual(
            key_store.get_active_keys(store_path=self.store), [key_a, key_b, key_c]
        )

    def test_add_keys_skips_duplicates(self):
        key_store.add_key("dup-key", store_path=self.store)
        added = key_store.add_keys("dup-key\nnew-key", store_path=self.store)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["id"], "k2")
        self.assertEqual(key_store.get_active_keys(store_path=self.store), ["dup-key", "new-key"])

    def test_add_keys_empty_blob_raises(self):
        with self.assertRaises(ValueError):
            key_store.add_keys("   \n  , ,", store_path=self.store)

    def test_add_keys_all_duplicates_raises(self):
        key_store.add_key("dup-key", store_path=self.store)
        with self.assertRaises(ValueError):
            key_store.add_keys("dup-key, dup-key", store_path=self.store)


class BulkAddEndpointTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = str(Path(self._tmp.name) / "gemini_keys_store.json")
        self._orig = key_store.KEY_STORE_PATH
        key_store.KEY_STORE_PATH = Path(self.store)
        self.client = TestClient(app)

    def tearDown(self):
        key_store.KEY_STORE_PATH = self._orig

    def test_bulk_add_multiple_keys_over_http(self):
        res = self.client.post(
            "/settings/keys/bulk",
            data={"keys": "AIza-aaa\nAIza-bbb,AIza-ccc"},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(len(body["added"]), 3)
        for entry in body["added"]:
            self.assertNotIn("AIza-", entry["key"])
        self.assertEqual(
            key_store.get_active_keys(), ["AIza-aaa", "AIza-bbb", "AIza-ccc"]
        )

    def test_bulk_add_all_duplicates_400(self):
        self.client.post("/settings/keys/bulk", data={"keys": "AIza-dup"})
        res = self.client.post("/settings/keys/bulk", data={"keys": "AIza-dup"})
        self.assertEqual(res.status_code, 400)

    def test_bulk_add_empty_400(self):
        res = self.client.post("/settings/keys/bulk", data={"keys": ""})
        self.assertEqual(res.status_code, 400)

    def test_bulk_add_never_leaks_raw_keys(self):
        self.client.post("/settings/keys/bulk", data={"keys": "AIza-top-secret"})
        res = self.client.get("/settings")
        self.assertNotIn("AIza-top-secret", res.text)
        self.assertIn("...cret", res.text)


class SettingsEndpointTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = str(Path(self._tmp.name) / "gemini_keys_store.json")
        self.client = TestClient(app)

    def _patch_store(self):
        self._orig = key_store.KEY_STORE_PATH
        key_store.KEY_STORE_PATH = Path(self.store)

    def tearDown(self):
        if hasattr(self, "_orig"):
            key_store.KEY_STORE_PATH = self._orig

    def test_add_list_delete_over_http(self):
        self._patch_store()
        res = self.client.post(
            "/settings/keys",
            data={"key": SECRET, "label": "http key"},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertNotIn(SECRET, res.text)
        key_id = body["added"]["id"]

        res = self.client.get("/settings")
        self.assertEqual(res.status_code, 200)
        self.assertIn(key_id, res.text)
        self.assertNotIn(SECRET, res.text)
        self.assertIn("..." + SECRET[-4:], res.text)

        res = self.client.delete(f"/settings/keys?key_id={key_id}")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(SECRET, res.text)

        res = self.client.get("/settings")
        self.assertNotIn(key_id, res.text)

    def test_delete_missing_id_returns_404(self):
        self._patch_store()
        res = self.client.delete("/settings/keys?key_id=k999")
        self.assertEqual(res.status_code, 404)
        self.assertIn("not found", res.json()["detail"])

    def test_delete_twice_returns_404(self):
        self._patch_store()
        res = self.client.post("/settings/keys", data={"key": SECRET})
        key_id = res.json()["added"]["id"]
        self.assertEqual(self.client.delete(f"/settings/keys?key_id={key_id}").status_code, 200)
        res = self.client.delete(f"/settings/keys?key_id={key_id}")
        self.assertEqual(res.status_code, 404)

    def test_add_empty_key_returns_400(self):
        self._patch_store()
        res = self.client.post("/settings/keys", data={"key": ""})
        self.assertEqual(res.status_code, 400)

    def test_raw_key_never_leaks_over_http(self):
        self._patch_store()
        self.client.post("/settings/keys", data={"key": SECRET})
        for path in ("/", "/settings", "/settings/keys"):
            res = self.client.get(path)
            self.assertNotIn(SECRET, res.text)


if __name__ == "__main__":
    unittest.main()
