import importlib
import json
import shutil
import sys
import tempfile
import unittest
import base64
from pathlib import Path
from unittest import mock

import harvester_ui
from harvester_core.config import load_config
from harvester_core.storage import save_json_atomic


class HarvesterUIBridgeTests(unittest.TestCase):
    def test_cli_and_core_import_without_severin(self):
        with mock.patch.dict(sys.modules, {"severin": None}):
            importlib.reload(importlib.import_module("harvester_core"))
            importlib.reload(importlib.import_module("harvester"))
            importlib.reload(harvester_ui)

    def test_unknown_action_is_rejected(self):
        with self.assertRaisesRegex(harvester_ui.BridgeError, "unknown bridge action"):
            harvester_ui.action_argv("shell", {})

    def test_malformed_messages_are_rejected(self):
        for frame in (None, "not json", "[]", '{"id": 1}', '{"id":1,"action":"inventory","data":[]}'):
            with self.subTest(frame=frame), self.assertRaises(harvester_ui.BridgeError):
                harvester_ui.decode_message(frame)

    def test_action_argv_has_fixed_executable_and_argument_shape(self):
        argv = harvester_ui.action_argv("get.movie", {"identifier": "Alien; rm -rf /"})
        self.assertEqual(argv[:3], [sys.executable, str(harvester_ui.HARVESTER_PATH), "api"])
        self.assertEqual(argv[3:], ["get", "movie", "Alien; rm -rf /"])
        with self.assertRaises(harvester_ui.BridgeError):
            harvester_ui.action_argv("list.movies", {"argv": ["refresh", "movie"]})
        with self.assertRaises(harvester_ui.BridgeError):
            harvester_ui.action_argv("get.movie", {"identifier": "--help"})

    def test_action_allowlist_is_derived_from_the_registry(self):
        self.assertEqual(harvester_ui.BRIDGE_ACTIONS,
                         frozenset({"__ping__", *harvester_ui.ACTION_REGISTRY}))
        with self.assertRaises(harvester_ui.BridgeError):
            harvester_ui.action_argv("actor.install_image", {"path": "/tmp/escape"})

    def test_manual_jpeg_install_uses_canonical_actor_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config({"state_dir": root / "state", "movie_root": root / "movies",
                                  "tv_root": root / "tv"}, environ={}, app_dir=root)
            save_json_atomic(config.state_path("movie_actor_queue.json"),
                             {"actors": {"Actor / Name": {"status": "ok"}}})
            jpeg = b"\xff\xd8\xffmanual-jpeg"
            payload = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
            with mock.patch("harvester_core.config.load_config", return_value=config), \
                    mock.patch("harvester_core.images.normalize_actor_image",
                               side_effect=AssertionError("JPEG must not require Pillow")):
                result = harvester_ui.install_actor_image(
                    {"identifier": "Actor / Name", "data_url": payload})
            destination = config.movie_root / ".actors" / "Actor___Name.jpg"
            self.assertEqual(destination.read_bytes(), jpeg)
            self.assertEqual(result["local_file"], str(destination))
            self.assertFalse((root / "escape").exists())

    def test_manual_install_rejects_unknown_actor_and_arbitrary_fields(self):
        with self.assertRaisesRegex(harvester_ui.BridgeError, "requires identifier"):
            harvester_ui.install_actor_image(
                {"identifier": "Nobody", "data_url": "data:image/jpeg;base64,/9j/", "path": "/tmp/x"})

    def test_ndjson_result_ignores_events(self):
        output = '\n'.join((
            '{"schema":1,"type":"event","event":"progress"}',
            '{"schema":1,"type":"result","ok":true,"result":{"items":[]}}',
        ))
        self.assertEqual(harvester_ui.parse_ndjson(output), {"items": []})

    def test_large_collection_is_published_outside_bridge_reply(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / ".cache" / "ui"
            items = [{"kind": "actor", "name": f"Actor {number:05d}"}
                     for number in range(16000)]
            with mock.patch.object(harvester_ui, "PROJECT_DIR", root), \
                    mock.patch.object(harvester_ui, "CACHE_DIR", cache):
                descriptor = harvester_ui.publish_collection(
                    "list.actors", {"missing": "image"}, {"items": items})
                other = harvester_ui.publish_collection(
                    "list.actors", {"status": "failed"}, {"items": []})
            reply = harvester_ui.make_reply(1, result=descriptor)
            self.assertLess(len(reply), 500)
            self.assertNotIn("Actor 15999", reply)
            self.assertNotEqual(descriptor["asset"], other["asset"])
            payload = json.loads(next(cache.glob("*" + descriptor["asset"].rsplit("-", 1)[-1])).read_text())
            self.assertEqual(payload["items"], items)
            self.assertEqual(payload["generation"], descriptor["generation"])

    def test_structured_error_becomes_bridge_error(self):
        with self.assertRaisesRegex(harvester_ui.BridgeError, "movie not found"):
            harvester_ui.parse_ndjson('{"schema":1,"type":"error","ok":false,"error":"movie not found"}')

    def test_malformed_ndjson_fails_cleanly(self):
        for output in ("not-json", '{"type":"mystery"}', '{"type":"event"}'):
            with self.subTest(output=output), self.assertRaises(harvester_ui.BridgeError):
                harvester_ui.parse_ndjson(output)


class HarvesterUICacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / ".cache" / "ui" / "snapshot.json"

    def tearDown(self):
        self.temporary.cleanup()

    def test_cache_json_write_is_atomic(self):
        harvester_ui.atomic_write_json(self.snapshot, {"version": 1, "views": {}})
        self.assertEqual(json.loads(self.snapshot.read_text()), {"version": 1, "views": {}})
        self.assertEqual(list(self.snapshot.parent.glob(".snapshot.json.*")), [])

    def test_incompatible_or_malformed_cache_is_a_miss(self):
        harvester_ui.atomic_write_json(self.snapshot, {"version": 999, "views": {}})
        self.assertIsNone(harvester_ui.read_snapshot(self.snapshot))
        self.snapshot.write_text("broken", encoding="utf-8")
        self.assertIsNone(harvester_ui.read_snapshot(self.snapshot))

    def test_cache_path_is_package_contained_and_deletion_is_harmless(self):
        harvester_ui.CACHE_DIR.relative_to(harvester_ui.PROJECT_DIR)
        shutil.rmtree(self.snapshot.parent, ignore_errors=True)
        self.assertIsNone(harvester_ui.read_snapshot(self.snapshot))

    def test_package_cache_refuses_symlink_nodes(self):
        cache_root = self.root / ".cache"
        cache_root.symlink_to(self.root / "elsewhere", target_is_directory=True)
        with mock.patch.object(harvester_ui, "PROJECT_DIR", self.root), \
                mock.patch.object(harvester_ui, "CACHE_DIR", cache_root / "ui"):
            with self.assertRaisesRegex(OSError, "symlinked UI cache"):
                harvester_ui._prepare_cache_directory()

    def test_host_and_page_package_ids_agree(self):
        page = (harvester_ui.PROJECT_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn(f'const PACKAGE_ID = "{harvester_ui.PACKAGE_ID}";', page)
        self.assertIn("asset://${PACKAGE_ID}/.cache/ui/collection-v", page)
        self.assertIn("requestCollection", page)

    def test_provider_profiles_are_rendered_as_profiles(self):
        page = (harvester_ui.PROJECT_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("value.providers", page)
        self.assertIn("profile.credential_requirements", page)
        self.assertIn("renderProvider(row)", page)


if __name__ == "__main__":
    unittest.main()
