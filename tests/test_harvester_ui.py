import importlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harvester_ui


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

    def test_ndjson_result_ignores_events(self):
        output = '\n'.join((
            '{"schema":1,"type":"event","event":"progress"}',
            '{"schema":1,"type":"result","ok":true,"result":{"items":[]}}',
        ))
        self.assertEqual(harvester_ui.parse_ndjson(output), {"items": []})

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
        self.assertIn("asset://${PACKAGE_ID}/.cache/ui/snapshot.json", page)


if __name__ == "__main__":
    unittest.main()
