import importlib
import json
import sys
import tempfile
import unittest
import base64
from pathlib import Path
from unittest import mock

import harvester_ui
from harvester_core.jobs import bulk
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
        self.assertEqual(
            harvester_ui.action_argv("rescan", {})[3:],
            ["rescan"],
        )
        self.assertEqual(
            harvester_ui.action_argv("inspect.movie", {"identifier": "/movies/Alien"})[3:],
            ["inspect", "movie", "/movies/Alien"],
        )
        self.assertIn("--artifacts", harvester_ui.action_argv("list.movies", {})[3:])
        self.assertNotIn("--group-directories",
                         harvester_ui.action_argv("list.movies", {"status": "failed"})[3:])
        self.assertIn("--group-directories",
                      harvester_ui.action_argv("list.movies", {"missing": "poster"})[3:])
        with self.assertRaises(harvester_ui.BridgeError):
            harvester_ui.action_argv("rescan", {"target": "actors"})

    def test_action_allowlist_is_derived_from_the_registry(self):
        self.assertEqual(harvester_ui.BRIDGE_ACTIONS,
                         frozenset({"__ping__", *harvester_ui.ACTION_REGISTRY}))
        with self.assertRaises(harvester_ui.BridgeError):
            harvester_ui.action_argv("actor.install_image", {"path": "/tmp/escape"})

    def test_bulk_workflow_is_semantic_bounded_and_preserves_artifacts(self):
        scope = {
            "asset": "asset://com.harvester.app/.cache/ui/collection-v1-abc.json",
            "count": 2, "generation": "generation", "version": 1,
        }
        argv = harvester_ui.action_argv(
            "bulk.workflow",
            {"workflow": "missing-posters", "scope": scope},
        )
        self.assertEqual(argv[3:5], ["bulk", "missing-posters"])
        self.assertIn("--scope-file", argv)
        self.assertNotIn("movie one", argv)
        for payload in (
                {"workflow": "shell", "scope": scope},
                {"workflow": "missing-posters", "scope": {**scope, "count": 1_000_001}},
                {"workflow": "missing-posters", "scope": {**scope, "asset": "asset://com.harvester.app/.cache/ui/../escape"}}):
            with self.subTest(payload=payload), self.assertRaises(harvester_ui.BridgeError):
                harvester_ui.action_argv("bulk.workflow", payload)

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

    def test_manual_install_rejects_a_source_sized_bridge_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config({"state_dir": root / "state", "movie_root": root / "movies",
                                  "tv_root": root / "tv"}, environ={}, app_dir=root)
            save_json_atomic(config.state_path("movie_actor_queue.json"),
                             {"actors": {"Actor": {"status": "ok"}}})
            source = b"\xff\xd8\xff" + (b"x" * 512_000)
            payload = "data:image/jpeg;base64," + base64.b64encode(source).decode()
            with mock.patch("harvester_core.config.load_config", return_value=config):
                with self.assertRaisesRegex(harvester_ui.BridgeError, "512 KB"):
                    harvester_ui.install_actor_image(
                        {"identifier": "Actor", "data_url": payload})

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
        self.collection = self.root / ".cache" / "ui" / "collection.json"

    def tearDown(self):
        self.temporary.cleanup()

    def test_cache_json_write_is_atomic(self):
        harvester_ui.atomic_write_json(self.collection, {"version": 1, "items": []})
        self.assertEqual(json.loads(self.collection.read_text()), {"version": 1, "items": []})
        self.assertEqual(list(self.collection.parent.glob(".collection.json.*")), [])

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

    def test_renderer_normalizes_actor_images_before_bridge_send(self):
        page = (harvester_ui.PROJECT_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("185 / image.width", page)
        self.assertIn("278 / image.height", page)
        self.assertIn('"image/jpeg"', page)
        self.assertIn("const dataUrl = await normalizeActorImage(file)", page)
        self.assertIn('classList.remove("busy")', page)
        self.assertIn("setStartupScanning(true)", page)
        self.assertIn('querySelector("#work-menu").disabled = scanning', page)
        self.assertIn('querySelector("#search").disabled = scanning', page)
        self.assertIn("showStartupRescanFailure(error)", page)
        self.assertIn("Work queues and Search remain unavailable", page)

    def test_bulk_drawer_and_workspace_share_navigation_safe_frozen_state(self):
        page = (harvester_ui.PROJECT_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('bulk: { job: null, drawerOpen: false }', page)
        self.assertIn('state.bulk.job = {', page)
        self.assertIn('scope,', page)
        self.assertIn('App.request("bulk.workflow", { workflow, scope }, false, true)', page)
        self.assertIn('if (state.workflow === "bulk")', page)
        self.assertIn('state.bulk.drawerOpen = false', page)
        self.assertNotIn('state.bulk.job = null', page)
        self.assertNotIn('completed: identities.length', page)
        self.assertIn('<progress aria-label="Bulk work in progress"></progress>', page)
        self.assertIn('writerActive()', page)
        self.assertIn('Re-fetch from web', page)


class BulkRecipeTests(unittest.TestCase):
    def test_lost_found_scans_before_materializing_nfo(self):
        config = mock.Mock(tmdb_api_key="key", tmdb_bearer_token=None)
        config.state_path.return_value = Path("cache.json")
        calls = []
        with mock.patch.object(bulk, "get_record", return_value={"local_target": "movie.nfo"}), \
                mock.patch("harvester_core.transport.transport_from_config", return_value=object()), \
                mock.patch("harvester_core.providers.tmdb.TMDBClient", return_value=object()), \
                mock.patch("harvester_core.jobs.movie_scan.run",
                           side_effect=lambda *a, **k: calls.append("scan") or {"processed": 1}), \
                mock.patch("harvester_core.jobs.movie_materialize.run",
                           side_effect=lambda *a, **k: calls.append("materialize") or
                           {"processed": 1, "counts": {"ok": 1}}):
            result = bulk.run(config, "lost-found", ["movie"], None)
        self.assertEqual(calls, ["scan", "materialize"])
        self.assertEqual(result["processed"], 1)

    def test_missing_actor_bulk_uses_transport_and_accounts_for_missing_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config({"state_dir": root / "state", "movie_root": root / "movies",
                                  "tv_root": root / "tv"}, environ={}, app_dir=root)
            config.movie_root.mkdir()
            class Response:
                headers = {"Content-Type": "image/jpeg"}
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self): return b"\xff\xd8actor"
            class RecordingTransport:
                user_agent = None
                def open(self, request, timeout=None):
                    self.user_agent = request.get_header("User-agent")
                    return Response()
            transport = RecordingTransport()
            def scan_with_one_source(*args, **kwargs):
                save_json_atomic(config.state_path("actor_thumb_urls_tmdb.json"),
                                 {"Has URL": ["https://images/actor.jpg"]})
                return {"processed": 2, "counts": {"ok": 1, "unresolved": 1}}
            with mock.patch("harvester_core.transport.transport_from_config",
                            return_value=transport), \
                    mock.patch("harvester_core.providers.tmdb.TMDBClient",
                               return_value=object()), \
                    mock.patch("harvester_core.jobs.movie_actor_scan.run",
                               side_effect=scan_with_one_source):
                result = bulk.run(config, "missing-actor-images",
                                  ["Has URL", "No URL"], None)
            self.assertEqual(transport.user_agent, "local-tmdb-actor-photo-gulper/1.0")
            self.assertEqual(result["processed"], 2)
            self.assertEqual(result["counts"]["image_unresolved_source"], 1)

    def test_missing_poster_reports_unresolved_target(self):
        config = mock.Mock()
        with mock.patch.object(bulk, "get_record", return_value={"local_target": "movie.nfo"}), \
                mock.patch("harvester_core.transport.transport_from_config", return_value=object()), \
                mock.patch("harvester_core.jobs.movie_materialize.run", return_value={
                    "processed": 1, "counts": {"poster_unresolved_target": 1}}):
            result = bulk.run(config, "missing-posters", ["movie"], None)
        self.assertFalse(result["ok"])
        self.assertIn("no safe poster target", result["message"])
        self.assertEqual(result["counts"]["poster_unresolved_target"], 1)

    def test_scope_rejects_oversized_aggregate_identities(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / ".cache" / "ui"
            cache.mkdir(parents=True)
            path = cache / "collection-v1-large.json"
            items = [{"name": "x" * (16 * 1024 * 1024 + 1)}]
            generation = __import__("hashlib").sha256(json.dumps(
                items, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()[:20]
            path.write_text(json.dumps({"version": 1, "generation": generation,
                                        "items": items}))
            config = mock.Mock(app_dir=root)
            with self.assertRaisesRegex(ValueError, "identity scope"):
                bulk.load_scope(config, "missing-actor-images", path, generation, 1)

    def test_movie_and_tv_renderers_use_artifact_inspection(self):
        page = (harvester_ui.PROJECT_DIR / "index.html").read_text(encoding="utf-8")
        css = (harvester_ui.PROJECT_DIR / "css" / "my.css").read_text(encoding="utf-8")
        self.assertIn('inspect: "inspect.movie"', page)
        self.assertIn('inspect: "inspect.show"', page)
        self.assertIn('artifactLine("Poster", detail.poster)', page)
        self.assertIn("manifest_identities", page)
        self.assertIn("renderRecordInspector(detail)", page)
        self.assertIn("row.grouped", page)
        self.assertIn("await rawRecords(detail.kind, rawIds)", page)
        self.assertIn("position: sticky", css)


if __name__ == "__main__":
    unittest.main()
