import importlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
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

    def test_artifact_preview_resolves_semantic_identity_not_renderer_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); movies = root / "movies"; tv = root / "tv"
            movies.mkdir(); tv.mkdir(); cache = root / ".cache" / "ui"
            config = load_config({"state_dir": root / "state", "movie_root": movies,
                                  "tv_root": tv}, environ={}, app_dir=root)
            actors = movies / ".actors"; actors.mkdir()
            mugshot = actors / "Actor.jpg"; mugshot.write_bytes(b"jpeg")
            movie = movies / "Movie"; movie.mkdir()
            nfo = movie / "movie.nfo"; nfo.write_text("<movie/>")
            poster = movie / "poster.jpg"; poster.write_bytes(b"poster")
            show = tv / "Show"; show.mkdir()
            show_poster = show / "poster.png"; show_poster.write_bytes(b"show poster")
            save_json_atomic(config.state_path("movie_actor_queue.json"),
                             {"actors": {"Actor": {"status": "ok"}}})
            save_json_atomic(config.state_path("movie_manifest_tmdb.json"), {"movies": {
                str(nfo): {"nfo_path": str(nfo), "poster_path": str(poster)}}})
            save_json_atomic(config.state_path("tv_show_urls_tvdb.json"),
                             {"shows": {str(show): {"status": "matched"}}})
            with mock.patch("harvester_core.config.load_config", return_value=config), \
                    mock.patch.object(harvester_ui, "PROJECT_DIR", root), \
                    mock.patch.object(harvester_ui, "CACHE_DIR", cache):
                actor = harvester_ui.publish_artifact_preview(
                    {"kind": "actor", "identifier": "Actor"})
                movie_result = harvester_ui.publish_artifact_preview(
                    {"kind": "movie", "identifier": str(nfo)})
                show_result = harvester_ui.publish_artifact_preview(
                    {"kind": "show", "identifier": str(show)})
                with self.assertRaisesRegex(harvester_ui.BridgeError, "semantic"):
                    harvester_ui.publish_artifact_preview(
                        {"kind": "actor", "identifier": "Actor", "path": "/etc/passwd"})
            self.assertTrue(actor["available"])
            self.assertTrue(movie_result["available"])
            self.assertTrue(show_result["available"])

    def test_missing_artifact_preview_is_text_only_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); movies = root / "movies"; tv = root / "tv"
            movies.mkdir(); tv.mkdir()
            config = load_config({"state_dir": root / "state", "movie_root": movies,
                                  "tv_root": tv}, environ={}, app_dir=root)
            save_json_atomic(config.state_path("movie_actor_queue.json"),
                             {"actors": {"Actor": {"status": "ok"}}})
            with mock.patch("harvester_core.config.load_config", return_value=config):
                self.assertEqual(harvester_ui.publish_artifact_preview(
                    {"kind": "actor", "identifier": "Actor"}), {"available": False})

    def test_ndjson_result_ignores_events(self):
        output = '\n'.join((
            '{"schema":1,"type":"event","event":"progress"}',
            '{"schema":1,"type":"result","ok":true,"result":{"items":[]}}',
        ))
        self.assertEqual(harvester_ui.parse_ndjson(output), {"items": []})

    def test_bulk_stream_delivers_event_before_terminal_result(self):
        program = (
            'import json,sys,time; '
            'print(json.dumps({"type":"event","event":"progress","id":"movie"}),flush=True); '
            'sys.stderr.write("verbose\\n"*20000); sys.stderr.flush(); time.sleep(.05); '
            'print(json.dumps({"type":"result","ok":True,"result":{"processed":1}}),flush=True)'
        )
        observed = []
        with mock.patch.object(harvester_ui, "action_argv", return_value=[sys.executable, "-c", program]):
            result = harvester_ui.run_streaming_action(
                "bulk.workflow", {}, lambda event: observed.append((event, time.monotonic())))
        self.assertEqual(result, {"processed": 1})
        self.assertEqual(observed[0][0]["id"], "movie")

    def test_selected_bulk_freezes_only_the_existing_logical_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); cache = root / ".cache" / "ui"; cache.mkdir(parents=True)
            items = [{"identifier": "other"}, {"grouped": True,
                     "manifest_identities": ["selected-a", "selected-b"]}]
            generation = __import__("hashlib").sha256(json.dumps(
                items, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()[:20]
            source = cache / "collection-v1-source.json"
            source.write_text(json.dumps({"version": 1, "generation": generation, "items": items}))
            scope = {"asset": "asset://com.harvester.app/.cache/ui/collection-v1-source.json",
                     "count": 2, "generation": generation, "version": 1}
            with mock.patch.object(harvester_ui, "PROJECT_DIR", root), \
                    mock.patch.object(harvester_ui, "CACHE_DIR", cache):
                argv = harvester_ui.action_argv(
                    "bulk.item", {"workflow": "missing-posters", "scope": scope, "index": 1})
            frozen = Path(argv[argv.index("--scope-file") + 1])
            self.assertEqual(json.loads(frozen.read_text())["items"], [items[1]])
            self.assertEqual(argv[-1], "1")

    def test_configuration_save_preserves_unmanaged_content_and_reports_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root / "keys_and_tokens.txt"
            path.write_text("# keep me\nUNRELATED=yes\nTMDB_API_KEY=old\n")
            values = {field: "" for field in harvester_ui.CONFIG_FIELDS}
            values["tmdb_api_key"] = "new"
            with mock.patch.object(harvester_ui, "PROJECT_DIR", root), \
                    mock.patch.dict("os.environ", {"TMDB_API_KEY": "environment"}, clear=False):
                result = harvester_ui.save_configuration({"values": values})
            content = path.read_text()
            self.assertIn("# keep me\nUNRELATED=yes\nTMDB_API_KEY=new\n", content)
            self.assertTrue(result["environment_overrides"]["tmdb_api_key"])
            self.assertNotIn("environment", content)

    def test_bulk_event_channel_is_session_scoped_and_always_completes(self):
        class App:
            reply = None
            def write(self, receipt, reply):
                self.reply = json.loads(reply)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); cache = root / ".cache" / "ui"
            def fail(_action, _data, publish, _process_key):
                publish({"type": "event", "event": "progress", "id": "one"})
                raise harvester_ui.BridgeError("failed after progress")
            def succeed(_action, _data, publish, _process_key):
                publish({"type": "event", "event": "progress", "id": "one"})
                return {"processed": 1}
            for session, operation, expected_ok in (("a" * 32, fail, False),
                                                     ("b" * 32, succeed, True)):
                app = App()
                message = json.dumps({"id": 7, "session": session,
                                      "action": "bulk.workflow", "data": {}})
                with mock.patch.object(harvester_ui, "PROJECT_DIR", root), \
                        mock.patch.object(harvester_ui, "CACHE_DIR", cache), \
                        mock.patch.object(harvester_ui, "run_streaming_action",
                                          side_effect=operation):
                    harvester_ui._run_bridge_job(app, object(), message)
                channel = json.loads((cache / f"events-{session}-7.json").read_text())
                self.assertTrue(channel["complete"])
                self.assertEqual(channel["events"][0]["id"], "one")
                self.assertEqual(app.reply["ok"], expected_ok)
            self.assertFalse((cache / "events-7.json").exists())

    def test_stopping_acquisition_keeps_prepared_inbox_items(self):
        from harvester_core.artifacts import RecordingCommitter, list_inbox, persist_preparation
        class Process:
            terminated = False
            def terminate(self): self.terminated = True
        class App:
            def write(self, receipt, reply): self.reply = json.loads(reply)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / "movies").mkdir(); (root / "tv").mkdir()
            config = load_config({"state_dir": root / "state", "movie_root": root / "movies",
                                  "tv_root": root / "tv"}, environ={}, app_dir=root)
            recorder = RecordingCommitter(); recorder.write(root / "movies" / "one.nfo", b"one")
            persist_preparation(config, "lost-found", ["one"], recorder)
            process = Process(); key = ("c" * 32, 9)
            harvester_ui._bulk_processes[key] = process
            app = App()
            harvester_ui._run_bridge_job(app, object(), json.dumps({
                "id": 10, "session": key[0], "action": "bulk.stop",
                "data": {"request_id": key[1]}}))
            harvester_ui._bulk_processes.pop(key, None)
            self.assertTrue(process.terminated)
            self.assertEqual(len(list_inbox(config)), 1)

    def test_concurrent_acquisition_does_not_block_different_inbox_apply(self):
        from harvester_core.artifacts import RecordingCommitter, persist_preparation
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); movies = root / "movies"; tv = root / "tv"
            movies.mkdir(); tv.mkdir(); (movies / "Reviewed").mkdir()
            config = load_config({"state_dir": root / "state", "movie_root": movies,
                                  "tv_root": tv}, environ={}, app_dir=root)
            target = movies / "Reviewed" / "movie.nfo"
            recorder = RecordingCommitter(); recorder.write(target, b"offline")
            plan = persist_preparation(config, "lost-found", ["reviewed"], recorder)
            # An active acquisition is deliberately not part of the library
            # commit lock; only simultaneous Apply operations serialize.
            harvester_ui._bulk_processes[("d" * 32, 4)] = mock.Mock()
            environ = {"HARVESTER_MOVIE_ROOT": str(movies),
                       "HARVESTER_TV_ROOT": str(tv),
                       "HARVESTER_STATE_DIR": str(root / "state")}
            try:
                with mock.patch.object(harvester_ui, "PROJECT_DIR", root), \
                        mock.patch.dict("os.environ", environ, clear=True):
                    result = harvester_ui.run_inbox_action(
                        "inbox.apply", {"item_id": plan["plan_id"]})
            finally:
                harvester_ui._bulk_processes.pop(("d" * 32, 4), None)
            self.assertEqual(result["applied"], 1)
            self.assertEqual(target.read_bytes(), b"offline")

    def test_apply_all_ready_continues_past_stale_items(self):
        from harvester_core.artifacts import (RecordingCommitter, list_inbox,
                                              persist_preparation)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); movies = root / "movies"; tv = root / "tv"
            movies.mkdir(); tv.mkdir()
            config = load_config({"state_dir": root / "state", "movie_root": movies,
                                  "tv_root": tv}, environ={}, app_dir=root)
            targets = [movies / name / "movie.nfo" for name in ("One", "Stale", "Three")]
            for target in targets:
                target.parent.mkdir()
                recorder = RecordingCommitter(); recorder.write(target, target.parent.name.encode())
                persist_preparation(config, "lost-found", [target.parent.name], recorder)
            targets[1].write_bytes(b"newer")
            environ = {"HARVESTER_MOVIE_ROOT": str(movies), "HARVESTER_TV_ROOT": str(tv),
                       "HARVESTER_STATE_DIR": str(root / "state")}
            with mock.patch.object(harvester_ui, "PROJECT_DIR", root), \
                    mock.patch.dict("os.environ", environ, clear=True):
                result = harvester_ui.run_inbox_action("inbox.apply_all", {})
            self.assertEqual(result, {"applied": 2, "discarded": 0,
                                      "needs_attention": 1, "failed": 0,
                                      "processed": 3})
            self.assertEqual(targets[1].read_bytes(), b"newer")
            self.assertEqual([item["state"] for item in list_inbox(config)],
                             ["needs_attention"])

    def test_retry_override_persists_clears_and_keeps_same_inbox_identity(self):
        from harvester_core.artifacts import RecordingCommitter, get_inbox_item, persist_preparation
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); movies = root / "movies"; tv = root / "tv"
            movies.mkdir(); tv.mkdir(); cache = root / ".cache" / "ui"
            config = load_config({"state_dir": root / "state", "movie_root": movies,
                                  "tv_root": tv}, environ={}, app_dir=root)
            identity = str(movies / "Ugly.2019" / "movie.nfo")
            save_json_atomic(config.state_path("movie_manifest_tmdb.json"), {"_meta": {}, "movies": {
                identity: {"status": "unresolved", "local_target": identity,
                           "title": "Ugly.2019", "year": 2019}}})
            plan = persist_preparation(config, "lost-found", [identity], RecordingCommitter())
            with mock.patch.object(harvester_ui, "PROJECT_DIR", root), \
                    mock.patch.object(harvester_ui, "CACHE_DIR", cache):
                argv = harvester_ui.action_argv("inbox.retry", {
                    "item_id": plan["plan_id"],
                    "query": {"title": "Clean Title", "year": "2019"}})
                first_id = get_inbox_item(config, plan["plan_id"])["item_id"]
                state = json.loads(config.state_path("movie_manifest_tmdb.json").read_text())
                self.assertEqual(state["movies"][identity]["query_override"],
                                 {"title": "Clean Title", "year": 2019})
                # A later host/config reload sees the same durable override.
                reloaded = load_config({"state_dir": root / "state", "movie_root": movies,
                                        "tv_root": tv}, environ={}, app_dir=root)
                self.assertEqual(json.loads(reloaded.state_path(
                    "movie_manifest_tmdb.json").read_text())["movies"][identity][
                        "query_override"]["title"], "Clean Title")
                harvester_ui._retrying_items.discard(plan["plan_id"])
                harvester_ui.action_argv("inbox.retry", {
                    "item_id": plan["plan_id"],
                    "query": {"title": "Changed Title", "year": ""}})
                changed = json.loads(config.state_path("movie_manifest_tmdb.json").read_text())
                self.assertEqual(changed["movies"][identity]["query_override"],
                                 {"title": "Changed Title", "year": None})
                harvester_ui._retrying_items.discard(plan["plan_id"])
                harvester_ui.action_argv("inbox.retry", {
                    "item_id": plan["plan_id"], "query": {"title": "", "year": ""}})
                harvester_ui._retrying_items.discard(plan["plan_id"])
            state = json.loads(config.state_path("movie_manifest_tmdb.json").read_text())
            self.assertNotIn("query_override", state["movies"][identity])
            self.assertEqual(first_id, plan["plan_id"])
            self.assertIn("--scope-file", argv)

    def test_apply_and_discard_reject_the_item_currently_being_retried(self):
        item_id = "e" * 32
        harvester_ui._retrying_items.add(item_id)
        try:
            for action in ("inbox.apply", "inbox.discard"):
                with self.subTest(action=action), mock.patch(
                        "harvester_core.config.load_config", return_value=mock.Mock()):
                    with self.assertRaisesRegex(harvester_ui.BridgeError, "being retried"):
                        harvester_ui.run_inbox_action(action, {"item_id": item_id})
        finally:
            harvester_ui._retrying_items.discard(item_id)

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
    def test_context_navigation_is_outside_scrollable_long_queue(self):
        page = (harvester_ui.PROJECT_DIR / "index.html").read_text(encoding="utf-8")
        ordinary = re.search(r'`<div class="context-nav">.*?class="context-scroll">.*?class="queue"',
                             page, re.DOTALL)
        inbox = re.search(r'`<div class="context-nav">.*?class="context-scroll">.*?data-inbox-row',
                          page, re.DOTALL)
        self.assertIsNotNone(ordinary)
        self.assertIsNotNone(inbox)
        css = (harvester_ui.PROJECT_DIR / "css" / "my.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.context-scroll\s*\{[^}]*overflow: auto")
        self.assertNotRegex(css, r"\.context-nav\s*\{[^}]*position: sticky")

    def test_overview_renders_before_background_inventory_reply(self):
        page = (harvester_ui.PROJECT_DIR / "index.html").read_text(encoding="utf-8")
        body = page.split("async function loadOverview()", 1)[1].split("async function start()", 1)[0]
        self.assertLess(body.index("renderOverview();"), body.index('await App.request("inventory"'))
        self.assertIn("generation !== state.generation", body)

    def test_renderers_request_only_semantic_artifact_previews(self):
        page = (harvester_ui.PROJECT_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('App.request("preview.artifact", { kind, identifier }', page)
        self.assertNotIn('preview.artifact", { path', page)
        self.assertIn("if (detail.poster?.present)", page)
        self.assertIn('detail.kind === "actor" && detail.local_file', page)

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

    @unittest.skipUnless(shutil.which("node"), "Node is unavailable for renderer regression")
    def test_grouped_row_uses_one_live_progress_unit_across_phase_events(self):
        page = (harvester_ui.PROJECT_DIR / "index.html").read_text(encoding="utf-8")
        function = re.search(
            r"      function advanceBulkProgress\(job, event\) \{.*?\n      \}",
            page, re.DOTALL,
        ).group(0)
        script = function + """
const job = {activity: '', stage: '', total: 1,
             identityOwners: new Map([['group-a', 0], ['group-b', 0]]),
             seenOwners: new Set(), liveProcessed: 0};
advanceBulkProgress(job, {event: 'progress', id: 'group-a'});
advanceBulkProgress(job, {event: 'prepared', id: 'group-b'});
if (`${job.liveProcessed} / ${job.total}` !== '1 / 1' || job.stage !== 'preparing') process.exit(1);
advanceBulkProgress(job, {event: 'progress', id: 'group-a', status: 'unresolved'});
if (job.activityStatus !== 'unresolved') process.exit(1);
const identityless = {activity: '', stage: '', total: 1, identityOwners: new Map(),
                      seenOwners: new Set(), liveProcessed: 0};
advanceBulkProgress(identityless, {event: 'inbox', id: 'plan', row_index: 0,
                                   status: 'needs_attention', label: 'Broken row'});
if (identityless.liveProcessed !== 1 || identityless.activity !== 'Broken row') process.exit(1);
"""
        subprocess.run(["node", "-e", script], check=True)
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
        self.assertIn('runBulk("bulk.workflow", { workflow, scope }', page)
        self.assertIn('if (state.workflow === "bulk")', page)
        self.assertIn('state.bulk.drawerOpen = false', page)
        self.assertNotIn('state.bulk.job = null', page)
        self.assertNotIn('completed: identities.length', page)
        self.assertIn('<progress aria-label="Bulk work in progress"></progress>', page)
        self.assertIn('writerActive()', page)
        self.assertIn('Re-fetch from web', page)
        self.assertIn('Re-fetch this item', page)
        self.assertNotIn('#apply-item, #discard-item', page.split('querySelectorAll("#scan-all', 1)[1].split(')', 1)[0])
        self.assertIn('runBulk("bulk.item", { workflow, scope, index }', page)
        self.assertIn("job.seenOwners.has(owner)", page)

    def test_candidate_click_supplies_frozen_row_and_issues_semantic_request(self):
        page = (harvester_ui.PROJECT_DIR / "index.html").read_text(encoding="utf-8")
        start = page.index("async function selectCandidate")
        end = page.index("\n      async function retryInboxItem", start)
        function = page[start:end]
        script = f"""
let captured;
async function runBulk(...args) {{ captured = args; }}
async function openInbox() {{}}
const state = {{inbox: {{items: []}}}};
function selectInboxItem() {{}}
{function}
(async () => {{
  const item = {{item_id:'plan', display_title:'Movie', identities:['movie.nfo']}};
  await selectCandidate(item, 2);
  if (captured[0] !== 'inbox.select_candidate') process.exit(1);
  if (captured[1].candidate_index !== 2 || captured[4][0].identifier !== 'movie.nfo') process.exit(2);
  if (state.inbox.items.length !== 0) process.exit(3);
}})().catch(() => process.exit(3));
"""
        subprocess.run(["node", "-e", script], check=True)

    def test_bulk_buttons_and_all_durable_inbox_outcomes_are_themed(self):
        css = (harvester_ui.PROJECT_DIR / "css" / "my.css").read_text(encoding="utf-8")
        page = (harvester_ui.PROJECT_DIR / "index.html").read_text(encoding="utf-8")
        shared_rule = re.search(r"\.dialog-actions button,.*?\{[^}]+\}", css, re.DOTALL).group(0)
        self.assertIn(".bulk-content button", shared_rule)
        self.assertIn(".bulk-empty button", shared_rule)
        self.assertRegex(css, r"#close-bulk\s*\{\s*font-size: 16px")
        for outcome, marker in (("ready", "🟢"), ("partial", "🟡"), ("failure", "⚠")):
            self.assertIn(f'{outcome}: "{marker}"', page)
        self.assertIn("item.summary?.outcome", page)

    def test_tv_candidate_is_derived_host_side_and_prepared_without_research(self):
        from harvester_core.artifacts import RecordingCommitter, get_inbox_item, persist_preparation
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); movies = root / "movies"; tv = root / "tv"
            movies.mkdir(); tv.mkdir(); show = tv / "The Office"; show.mkdir()
            config = load_config({"state_dir": root / "state", "movie_root": movies,
                                  "tv_root": tv}, environ={}, app_dir=root)
            candidates = [{"tvdb_id": 101, "name": "The Office", "year": 2001},
                          {"tvdb_id": 202, "name": "The Office", "year": 2005}]
            save_json_atomic(config.state_path("tv_show_urls_tvdb.json"), {"_meta": {}, "shows": {
                str(show): {"status": "ambiguous", "folder_name": show.name,
                            "query_title": "The Office", "query_year": None,
                            "local_target": str(show), "candidates": candidates}}})
            plan = persist_preparation(config, "ambiguous-tv", [str(show)], RecordingCommitter(),
                                       state="needs_attention", summary={
                                           "provider_results": [{"candidates": candidates}]})
            cache = root / ".cache" / "ui"
            with mock.patch.object(harvester_ui, "PROJECT_DIR", root), \
                    mock.patch.object(harvester_ui, "CACHE_DIR", cache), \
                    mock.patch("harvester_core.config.load_config", return_value=config):
                argv = harvester_ui.action_argv("inbox.select_candidate", {
                    "item_id": plan["plan_id"], "candidate_index": 1})
                with self.assertRaises(harvester_ui.BridgeError):
                    harvester_ui.action_argv("inbox.select_candidate", {
                        "item_id": plan["plan_id"], "candidate_index": 1, "tvdb_id": 999})
            self.assertIn("--scope-file", argv)
            frozen = json.loads(config.state_path("tv_show_urls_tvdb.json").read_text())
            self.assertEqual(frozen["shows"][str(show)]["query_override"], {"tvdb_id": 202})

            provider = mock.Mock()
            provider.get.return_value = ({"id": 202, "name": "The Office",
                                          "firstAired": "2005-03-24"}, False)
            with mock.patch("harvester_core.providers.tvdb.TVDBClient", return_value=provider), \
                    mock.patch("harvester_core.transport.transport_from_config",
                               return_value=object()):
                bulk.run_scoped(config, "ambiguous-tv", [{
                    "identities": [str(show)], "display_title": "The Office",
                    "local_target": str(show)}], 1)
            item = get_inbox_item(config, plan["plan_id"])
            record = json.loads(config.state_path("tv_show_urls_tvdb.json").read_text())["shows"][str(show)]
            self.assertEqual(record["status"], "matched")
            self.assertEqual(record["tvdb_id"], 202)
            self.assertEqual(record["match"]["method"], "human_selected_tvdb_id")
            self.assertEqual(item["state"], "ready")
            self.assertIn("show.nfo", [Path(action["path"]).name for action in item["actions"]])
            self.assertEqual(provider.get.call_args.args[0], "/series/202/extended")

    def test_candidate_selection_reselects_stable_item_and_merges_inferred_query(self):
        page = (harvester_ui.PROJECT_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("const query = { ...(item.query?.inferred || {}), ...(item.query?.override || {}) };", page)
        start = page.index("async function selectCandidate")
        end = page.index("\n      function bindInboxImage", start)
        function = page[start:end]
        script = f"""
let selected = -1;
async function runBulk() {{}}
const state = {{inbox: {{items: []}}}};
async function openInbox() {{ state.inbox.items = [{{item_id:'stable'}}]; }}
function selectInboxItem(index) {{ selected = index; }}
{function}
(async () => {{
  await selectCandidate({{item_id:'stable', display_title:'Movie', identities:['movie.nfo']}}, 0);
  if (selected !== 0) process.exit(1);
  const inferred = {{title:'Visible title', year:2024}};
  const override = {{tmdb_id:7}};
  const query = {{...inferred, ...override}};
  if (query.title !== 'Visible title' || query.year !== 2024) process.exit(2);
}})().catch(() => process.exit(3));
"""
        subprocess.run(["node", "-e", script], check=True)

    def test_search_inspector_uses_semantic_refetch_and_manual_image_requests(self):
        page = (harvester_ui.PROJECT_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('runBulk("item.refetch", { kind: row.kind, identifier: row.identifier }', page)
        self.assertIn('App.request("item.install_image", { kind: row.kind, identifier: row.identifier, data_url: dataUrl }', page)
        self.assertIn('Drop ${noun} here', page)
        self.assertIn('🟢 Prepared — <button id="search-open-inbox"', page)
        request = page[page.index('App.request("item.install_image"'):
                       page.index('App.request("item.install_image"') + 180]
        self.assertNotIn("path", request)

    def test_configuration_cancel_explicitly_closes_without_saving(self):
        page = (harvester_ui.PROJECT_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="cancel-configuration" type="button"', page)
        self.assertNotIn('<form method="dialog">', page)
        start = page.index('document.querySelector("#cancel-configuration").onclick')
        handler = page[start:page.index("\n      };", start) + len("\n      };")]
        script = f"""
let closed = false, saved = false;
const cancel = {{}};
const configuration = {{close() {{ closed = true; }}}};
const document = {{querySelector(value) {{
  if (value === '#cancel-configuration') return cancel;
  if (value === '#configuration') return configuration;
  saved = true;
}}}};
{handler}
cancel.onclick();
if (!closed || saved) process.exit(1);
"""
        subprocess.run(["node", "-e", script], check=True)


class BulkRecipeTests(unittest.TestCase):
    def test_movie_nfo_only_materialization_never_inspects_a_poster_target(self):
        from harvester_core.artifacts import RecordingCommitter
        from harvester_core.jobs import movie_materialize
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); movies = root / "movies"; tv = root / "tv"
            movies.mkdir(); tv.mkdir(); folder = movies / "No Poster"; folder.mkdir()
            config = load_config({"state_dir": root / "state", "movie_root": movies,
                                  "tv_root": tv}, environ={}, app_dir=root)
            nfo = folder / "movie.nfo"
            save_json_atomic(config.state_path("movie_manifest_tmdb.json"), {"movies": {
                str(nfo): {"status": "ok", "nfo_path": str(nfo), "poster_path": None,
                           "nfo": {"title": "No Poster"}, "poster_url": "https://unused"}}})
            recorder = RecordingCommitter()
            result = movie_materialize.run(config, targets=[str(nfo)], write_nfo=True,
                                           write_poster=False, committer=recorder,
                                           downloader=lambda _url: self.fail("poster downloaded"))
            self.assertEqual(result["planned_counts"], {"planned": 1})
            self.assertEqual([Path(action["path"]).name for action in recorder.actions],
                             ["movie.nfo"])

    def test_search_refetch_derives_movie_and_show_recipes_from_durable_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); movies = root / "movies"; tv = root / "tv"
            movies.mkdir(); tv.mkdir(); movie = movies / "Movie"; show = tv / "Show"
            movie.mkdir(); show.mkdir(); movie_nfo = movie / "movie.nfo"
            movie_nfo.write_bytes(b"existing nfo")
            (movie / "poster.jpg").write_bytes(b"existing movie poster")
            (show / "show.nfo").write_bytes(b"existing show nfo")
            (show / "poster.jpg").write_bytes(b"existing show poster")
            config = load_config({"state_dir": root / "state", "movie_root": movies,
                                  "tv_root": tv}, environ={}, app_dir=root)
            save_json_atomic(config.state_path("movie_manifest_tmdb.json"), {"movies": {
                str(movie_nfo): {"status": "ok", "local_target": str(movie_nfo),
                                 "nfo_path": str(movie_nfo), "poster_path": None,
                                 "nfo": {"title": "Movie"}}}})
            save_json_atomic(config.state_path("tv_show_urls_tvdb.json"), {"shows": {
                str(show): {"status": "matched", "local_target": str(show),
                            "folder_name": "Show", "tvdb_id": 7, "nfo": {"title": "Show"}}}})
            cache = root / ".cache" / "ui"
            with mock.patch.object(harvester_ui, "PROJECT_DIR", root), \
                    mock.patch.object(harvester_ui, "CACHE_DIR", cache), \
                    mock.patch("harvester_core.config.load_config", return_value=config):
                movie_argv = harvester_ui.action_argv(
                    "item.refetch", {"kind": "movie", "identifier": str(movie_nfo)})
                show_argv = harvester_ui.action_argv(
                    "item.refetch", {"kind": "show", "identifier": str(show)})
                for bad in ({"kind": "movie", "identifier": str(movie_nfo),
                             "workflow": "missing-posters"},
                            {"kind": "show", "identifier": str(show), "path": "/tmp/x"}):
                    with self.assertRaises(harvester_ui.BridgeError):
                        harvester_ui.action_argv("item.refetch", bad)
            self.assertEqual(movie_argv[4], "unresolved-movies")
            self.assertEqual(show_argv[4], "tv-errors")
            def movie_scan(*_args, **_kwargs):
                state = json.loads(config.state_path("movie_manifest_tmdb.json").read_text())
                state["movies"][str(movie_nfo)]["nfo"] = {"title": "Movie"}
                save_json_atomic(config.state_path("movie_manifest_tmdb.json"), state)
                return {"processed": 1, "counts": {"ok": 1}}

            def show_scan(*_args, **_kwargs):
                state = json.loads(config.state_path("tv_show_urls_tvdb.json").read_text())
                state["shows"][str(show)].update({"status": "matched", "tvdb_id": 7,
                                                  "nfo": {"title": "Show"}, "assets": {}})
                save_json_atomic(config.state_path("tv_show_urls_tvdb.json"), state)
                return {"processed": 1, "status_counts": {"matched": 1}}

            for argv, scanner in ((movie_argv, movie_scan), (show_argv, show_scan)):
                scope_path = Path(argv[argv.index("--scope-file") + 1])
                generation = argv[argv.index("--generation") + 1]
                workflow = argv[4]
                items = bulk.load_scope_items(config, workflow, scope_path, generation, 1)
                patches = (mock.patch("harvester_core.providers.tmdb.TMDBClient",
                                      return_value=object()),
                           mock.patch("harvester_core.providers.tvdb.TVDBClient",
                                      return_value=object()),
                           mock.patch("harvester_core.transport.transport_from_config",
                                      return_value=object()),
                           mock.patch("harvester_core.jobs.movie_scan.run",
                                      side_effect=scanner if workflow == "unresolved-movies" else None),
                           mock.patch("harvester_core.jobs.tv_scan.run",
                                      side_effect=scanner if workflow == "tv-errors" else None))
                with patches[0], patches[1], patches[2], patches[3], patches[4]:
                    bulk.run_scoped(config, workflow, items, 1)
            from harvester_core.artifacts import list_inbox
            prepared = list_inbox(config)
            self.assertEqual(len(prepared), 2)
            self.assertTrue(all(item["state"] == "ready" for item in prepared))
            self.assertEqual(movie_nfo.read_bytes(), b"existing nfo")
            self.assertEqual((movie / "poster.jpg").read_bytes(), b"existing movie poster")
            self.assertEqual((show / "show.nfo").read_bytes(), b"existing show nfo")
            self.assertEqual((show / "poster.jpg").read_bytes(), b"existing show poster")

    def test_search_manual_images_prepare_same_inbox_item_without_library_writes(self):
        from harvester_core.artifacts import list_inbox
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); movies = root / "movies"; tv = root / "tv"
            movies.mkdir(); tv.mkdir(); movie = movies / "Movie"; show = tv / "Show"
            movie.mkdir(); show.mkdir(); movie_nfo = movie / "movie.nfo"
            movie_nfo.write_bytes(b"existing nfo")
            (movie / "poster.jpg").write_bytes(b"existing movie poster")
            (show / "show.nfo").write_bytes(b"existing show nfo")
            (show / "poster.jpg").write_bytes(b"existing show poster")
            config = load_config({"state_dir": root / "state", "movie_root": movies,
                                  "tv_root": tv}, environ={}, app_dir=root)
            save_json_atomic(config.state_path("movie_manifest_tmdb.json"), {"movies": {
                str(movie_nfo): {"status": "ok", "local_target": str(movie_nfo),
                                 "nfo_path": str(movie_nfo), "poster_path": None}}})
            save_json_atomic(config.state_path("tv_show_urls_tvdb.json"), {"shows": {
                str(show): {"status": "matched", "local_target": str(show)}}})
            save_json_atomic(config.state_path("movie_actor_queue.json"), {"actors": {
                "Actor": {"status": "ok"}}})
            jpeg = b"\xff\xd8\xffprepared"
            payload = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
            with mock.patch("harvester_core.config.load_config", return_value=config), \
                    mock.patch.object(harvester_ui, "PROJECT_DIR", root):
                results = [harvester_ui.run_action("item.install_image", {
                    "kind": kind, "identifier": identity, "data_url": payload})
                    for kind, identity in (("movie", str(movie_nfo)),
                                           ("show", str(show)), ("actor", "Actor"))]
                again = harvester_ui.run_action("item.install_image", {
                    "kind": "movie", "identifier": str(movie_nfo), "data_url": payload})
                with self.assertRaises(harvester_ui.BridgeError):
                    harvester_ui.run_action("item.install_image", {
                        "kind": "movie", "identifier": str(movie_nfo),
                        "data_url": payload, "path": "/tmp/chosen-by-js"})
            self.assertEqual(results[0]["item_id"], again["item_id"])
            self.assertEqual(len(list_inbox(config)), 3)
            self.assertEqual(movie_nfo.read_bytes(), b"existing nfo")
            self.assertEqual((movie / "poster.jpg").read_bytes(), b"existing movie poster")
            self.assertEqual((show / "show.nfo").read_bytes(), b"existing show nfo")
            self.assertEqual((show / "poster.jpg").read_bytes(), b"existing show poster")
            self.assertFalse((movies / ".actors" / "Actor.jpg").exists())

    def test_tv_repairs_prepare_apply_and_inspect_missing_artifacts(self):
        from harvester_core.api import inspect_item
        from harvester_core.artifacts import apply_inbox_item, list_inbox
        from harvester_core.storage import load_json
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); movies = root / "movies"; tv = root / "tv"
            movies.mkdir(); tv.mkdir()
            config = load_config({"state_dir": root / "state", "movie_root": movies,
                                  "tv_root": tv}, environ={}, app_dir=root)
            ambiguous = tv / "Ambiguous"; poster_show = tv / "Poster"
            ambiguous.mkdir(); poster_show.mkdir()
            save_json_atomic(config.state_path("tv_show_urls_tvdb.json"), {"shows": {
                str(ambiguous): {"status": "ambiguous", "folder_name": "Ambiguous",
                                 "query_title": "Ambiguous", "query_year": 2020,
                                 "nfo": None, "assets": None},
                str(poster_show): {"status": "matched", "folder_name": "Poster",
                                   "query_title": "Poster", "query_year": 2021,
                                   "tvdb_id": 22, "nfo": {"title": "Poster"},
                                   "assets": {"poster_url": "https://poster"}},
            }})

            def resolve(*_args, **_kwargs):
                state = load_json(config.state_path("tv_show_urls_tvdb.json"))
                record = state["shows"][str(ambiguous)]
                record.update({"status": "matched", "tvdb_id": 11,
                               "nfo": {"title": "Ambiguous"}, "assets": {}})
                save_json_atomic(config.state_path("tv_show_urls_tvdb.json"), state)
                return {"processed": 1, "status_counts": {"matched": 1}}

            patches = (mock.patch("harvester_core.providers.tvdb.TVDBClient", return_value=object()),
                       mock.patch("harvester_core.transport.transport_from_config", return_value=object()),
                       mock.patch("harvester_core.jobs.tv_scan.run", side_effect=resolve),
                       mock.patch("harvester_core.jobs.tv_materialize.download_bytes",
                                  return_value=(b"\xff\xd8poster", "image/jpeg")))
            with patches[0], patches[1], patches[2], patches[3]:
                bulk.run_scoped(config, "ambiguous-tv", [{
                    "identities": [str(ambiguous)], "display_title": "Ambiguous",
                    "local_target": str(ambiguous)}], 1)
                nfo_item = list_inbox(config)[0]
                self.assertEqual(nfo_item["state"], "ready")
                self.assertEqual([Path(action["path"]).name for action in nfo_item["actions"]
                                  if action["action"] == "write"], ["show.nfo"])
                apply_inbox_item(config, nfo_item["item_id"])
                self.assertTrue(inspect_item(config, "show", str(ambiguous))["nfo"]["present"])

                bulk.run_scoped(config, "missing-tv-posters", [{
                    "identities": [str(poster_show)], "display_title": "Poster",
                    "local_target": str(poster_show)}], 1)
                poster_item = list_inbox(config)[0]
                self.assertEqual(poster_item["state"], "ready")
                apply_inbox_item(config, poster_item["item_id"])
                detail = inspect_item(config, "show", str(poster_show))
                self.assertTrue(detail["poster"]["present"])
                self.assertEqual(Path(detail["poster"]["path"]).name, "poster.jpg")

    def test_opportunistic_nfo_failure_overrides_resolved_identity(self):
        result = {"counts": {"identity_matched": 1, "nfo_error": 1},
                  "phase_results": {"nfo": {"show": {
                      "status": "error", "error": "cannot render NFO"}}}}
        attention, reason = bulk._scoped_artifact_outcome("ambiguous-tv", result)
        self.assertTrue(attention)
        self.assertEqual(reason, "cannot render NFO")

    def test_opportunistic_nfo_requires_planned_or_existing_artifact(self):
        result = {"counts": {"identity_ok": 1, "nfo_skipped": 1},
                  "phase_results": {"nfo": {"movie": {"status": "skipped"}}}}
        attention, reason = bulk._scoped_artifact_outcome("unresolved-movies", result)
        self.assertTrue(attention)
        self.assertEqual(reason, "No nfo artifact was prepared")

    def test_movie_query_override_controls_tmdb_without_changing_local_identity(self):
        from harvester_core.jobs import movie_scan
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); movies = root / "movies"; movies.mkdir()
            config = load_config({"state_dir": root / "state", "movie_root": movies,
                                  "tv_root": root / "tv"}, environ={}, app_dir=root)
            identity = str(movies / "Ugly.Name.2019" / "movie.nfo")
            save_json_atomic(config.state_path("movie_manifest_tmdb.json"), {"_meta": {}, "movies": {
                identity: {"status": "unresolved", "local_target": identity,
                           "nfo_path": identity, "poster_path": None,
                           "title": "Ugly.Name.2019.WEBRip", "original_title": None,
                           "year": 2019, "imdb_id": "tt-original", "local_tmdb_id": 99,
                           "query_override": {"title": "Fractured", "year": 2019}}}})
            queries = []
            provider = mock.Mock()
            provider.get.side_effect = lambda path, _params: ({"images": {}} if path == "/configuration" else {})
            with mock.patch.object(movie_scan, "discover_movies", return_value={}), \
                    mock.patch.object(movie_scan, "resolve_movie_tmdb_id",
                                      side_effect=lambda _provider, query: queries.append(query) or {
                                          "ok": False, "reason": "test", "top": []}):
                movie_scan.run(config, provider, refresh=True, targets=[identity])
            self.assertEqual(queries[0]["title"], "Fractured")
            self.assertEqual(queries[0]["year"], 2019)
            self.assertIsNone(queries[0]["imdb_id"])
            record = json.loads(config.state_path("movie_manifest_tmdb.json").read_text())["movies"][identity]
            self.assertEqual(record["local_target"], identity)
            self.assertEqual(record["title"], "Ugly.Name.2019.WEBRip")

    def test_tv_and_actor_overrides_reach_resolvers_without_renaming_identity(self):
        from harvester_core.jobs import movie_actor_scan, tv_scan
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); movies = root / "movies"; tv = root / "tv"
            movies.mkdir(); tv.mkdir()
            config = load_config({"state_dir": root / "state", "movie_root": movies,
                                  "tv_root": tv}, environ={}, app_dir=root)
            show = tv / "The.Show.Name.2024"; show.mkdir()
            tv_manifest = {"_meta": {}, "shows": {str(show): {
                "status": "not_found", "folder_name": show.name,
                "query_title": "The.Show.Name", "query_year": 2024,
                "query_override": {"title": "The Show Name", "year": 2024}}}}
            tv_queries = []
            with mock.patch.object(tv_scan, "load_or_merge_manifest",
                                   return_value=(tv_manifest, [show], 0)), \
                    mock.patch.object(tv_scan, "resolve_tvdb_series",
                                      side_effect=lambda _p, title, year: tv_queries.append(
                                          (title, year)) or {"ok": False, "status": "not_found"}):
                tv_scan.run(config, mock.Mock(), refresh=True, retry_not_found=True,
                            targets=[str(show)], sleep_between_shows=0)
            self.assertEqual(tv_queries, [("The Show Name", 2024)])
            self.assertIn(str(show), tv_manifest["shows"])

            queue = {"_meta": {}, "actors": {"Bad / Actor": {
                "status": "failed", "contexts": [],
                "query_override": {"name": "Good Actor"}}}}
            actor_queries = []
            with mock.patch.object(movie_actor_scan, "make_actor_work_queue", return_value=queue), \
                    mock.patch.object(movie_actor_scan, "get_tmdb_image_base",
                                      return_value=("https://image/", ["w185"])), \
                    mock.patch.object(movie_actor_scan, "resolve_actor_from_contexts",
                                      side_effect=lambda _p, name, *args: actor_queries.append(name) or {
                                          "ok": False, "reason": "test"}):
                movie_actor_scan.run(config, mock.Mock(), refresh=True, retry_failed=True,
                                     targets=["Bad / Actor"])
            self.assertEqual(actor_queries, ["Good Actor"])
            self.assertIn("Bad / Actor", queue["actors"])

    def test_scoped_artifact_result_controls_ready_state_after_provider_success(self):
        from harvester_core.artifacts import RecordingCommitter, list_inbox
        cases = (
            ("missing-actor-images", "actor", "Actor", "image", "failed", "download broke"),
            ("missing-posters", "movie", "movie.nfo", "poster", "error", "poster broke"),
        )
        for workflow, kind, identity_name, phase, failure, reason in cases:
            for succeeds in (False, True):
                with self.subTest(workflow=workflow, succeeds=succeeds), \
                        tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    config = load_config({"state_dir": root / "state",
                                          "movie_root": root / "movies",
                                          "tv_root": root / "tv"}, environ={}, app_dir=root)
                    config.movie_root.mkdir(); config.tv_root.mkdir()
                    if kind == "actor":
                        identity = identity_name
                        save_json_atomic(config.state_path("movie_actor_queue.json"), {
                            "actors": {identity: {"status": "ok"},
                                       "Unrelated": {"status": "failed"}}})
                    else:
                        identity = str(config.movie_root / "Movie" / identity_name)
                        save_json_atomic(config.state_path("movie_manifest_tmdb.json"), {
                            "movies": {identity: {"status": "ok", "local_target": identity},
                                       "unrelated.nfo": {"status": "unresolved"}}})

                    def prepared(*_args):
                        recorder = RecordingCommitter()
                        if succeeds:
                            recorder.write(config.movie_root / "prepared" / f"{phase}.jpg",
                                           b"artifact")
                        plan = bulk.persist_preparation(config, workflow, [identity], recorder)
                        status = "planned" if succeeds else failure
                        details = {"status": status}
                        if not succeeds:
                            details["error"] = reason
                        return {"processed": 1, "counts": {f"{phase}_{status}": 1},
                                "phase_results": {phase: {str(identity): details}},
                                "preparation": plan, "message": "Finished"}

                    item = {"identities": [identity], "display_title": str(identity),
                            "local_target": str(identity)}
                    with mock.patch.object(bulk, "run", side_effect=prepared):
                        bulk.run_scoped(config, workflow, [item], 1)
                    inbox = list_inbox(config)[0]
                    self.assertEqual(inbox["state"],
                                     "ready" if succeeds else "needs_attention")
                    self.assertEqual(sum(action["action"] == "write"
                                         for action in inbox["actions"]), int(succeeds))
                    if not succeeds:
                        self.assertEqual(inbox["reason"], reason)

    def test_combined_preserves_augmented_and_planned_artifact_counts(self):
        combined = bulk._combined(("image", {
            "counts": {"unresolved_source": 1, "failed": 9},
            "planned_counts": {"planned": 2, "failed": 1, "exists": 3},
        }))
        self.assertEqual(combined["counts"], {
            "image_unresolved_source": 1, "image_failed": 9,
            "image_planned": 2, "image_exists": 3})

    def test_inbox_state_uses_only_scoped_provider_record(self):
        from harvester_core.artifacts import RecordingCommitter, list_inbox
        for scoped_status, unrelated_status, expected in (
                ("ok", "unresolved", "ready"),
                ("unresolved", "ok", "needs_attention")):
            with self.subTest(scoped_status=scoped_status), \
                    tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = load_config({"state_dir": root / "state", "movie_root": root / "movies",
                                      "tv_root": root / "tv"}, environ={}, app_dir=root)
                config.movie_root.mkdir(); config.tv_root.mkdir()
                scoped = str(config.movie_root / "Scoped" / "movie.nfo")
                unrelated = str(config.movie_root / "Other" / "movie.nfo")
                save_json_atomic(config.state_path("movie_manifest_tmdb.json"), {"movies": {
                    scoped: {"status": scoped_status, "local_target": scoped,
                             "reason": "scoped unresolved", "candidates": ["Scoped candidate"]},
                    unrelated: {"status": unrelated_status, "local_target": unrelated,
                                "reason": "unrelated problem"}}})
                def prepared(*_args):
                    recorder = RecordingCommitter()
                    recorder.write(Path(scoped), b"nfo")
                    plan = bulk.persist_preparation(config, "lost-found", [scoped], recorder)
                    # Deliberately aggregate both records to reproduce the scanner
                    # summary that must not classify this scoped Inbox item.
                    return {"processed": 2, "counts": {"identity_ok": 1,
                            "identity_unresolved": 1, "nfo_planned": 1},
                            "preparation": plan,
                            "message": "Finished"}
                item = {"identities": [scoped], "display_title": "Scoped",
                        "local_target": scoped}
                with mock.patch.object(bulk, "run", side_effect=prepared):
                    bulk.run_scoped(config, "lost-found", [item], 1)
                inbox = list_inbox(config)
                self.assertEqual(inbox[0]["state"], expected)
                if expected == "needs_attention":
                    self.assertEqual(inbox[0]["reason"], "scoped unresolved")

    def test_first_item_enters_inbox_before_multi_item_producer_finishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config({"state_dir": root / "state", "movie_root": root / "movies",
                                  "tv_root": root / "tv"}, environ={}, app_dir=root)
            config.movie_root.mkdir(); config.tv_root.mkdir()
            calls = []
            def prepare(_config, workflow, identities, reporter):
                if calls:
                    self.assertEqual(len(__import__("harvester_core.artifacts", fromlist=["list_inbox"]).list_inbox(config)), 1)
                recorder = __import__("harvester_core.artifacts", fromlist=["RecordingCommitter"]).RecordingCommitter()
                recorder.write(config.movie_root / identities[0] / "movie.nfo", b"prepared")
                plan = bulk.persist_preparation(config, workflow, identities, recorder)
                calls.append(identities[0])
                return {"processed": 1, "counts": {}, "preparation": plan,
                        "message": "prepared"}
            items = [{"identities": ["one"], "display_title": "One", "local_target": None},
                     {"identities": ["two"], "display_title": "Two", "local_target": None}]
            with mock.patch.object(bulk, "run", side_effect=prepare):
                result = bulk.run_scoped(config, "lost-found", items, 2)
            self.assertEqual(result["processed"], 2)
            self.assertEqual(len(__import__("harvester_core.artifacts", fromlist=["list_inbox"]).list_inbox(config)), 2)

    def test_producer_error_becomes_attention_item_and_does_not_disappear(self):
        from harvester_core.artifacts import list_inbox
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config({"state_dir": root / "state", "movie_root": root / "movies",
                                  "tv_root": root / "tv"}, environ={}, app_dir=root)
            config.movie_root.mkdir(); config.tv_root.mkdir()
            item = {"identities": ["unresolved"], "display_title": "Unresolved",
                    "local_target": None}
            with mock.patch.object(bulk, "run", side_effect=RuntimeError("provider unavailable")):
                result = bulk.run_scoped(config, "lost-found", [item], 1)
            self.assertFalse(result["ok"])
            inbox = list_inbox(config)
            self.assertEqual(inbox[0]["state"], "needs_attention")
            self.assertIn("provider unavailable", inbox[0]["reason"])

    def test_grouped_scope_terminal_processed_uses_logical_row_count(self):
        underlying = {"processed": 2, "counts": {"identity_ok": 2},
                      "message": "Finished"}
        with mock.patch.object(bulk, "run", return_value=underlying):
            result = bulk.run_scoped(mock.Mock(), "missing-posters",
                                     ["group-a", "group-b"], 1)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["counts"]["scoped_identities"], 2)
        self.assertEqual(result["counts"]["identity_ok"], 2)

    def test_lost_found_scans_before_materializing_nfo(self):
        config = mock.Mock(tmdb_api_key="key", tmdb_bearer_token=None)
        config.state_path.return_value = Path("cache.json")
        calls = []
        committers = []
        with mock.patch.object(bulk, "get_record", return_value={"local_target": "movie.nfo"}), \
                mock.patch("harvester_core.transport.transport_from_config", return_value=object()), \
                mock.patch("harvester_core.providers.tmdb.TMDBClient", return_value=object()), \
                mock.patch("harvester_core.jobs.movie_scan.run",
                           side_effect=lambda *a, **k: calls.append("scan") or {"processed": 1}), \
                mock.patch("harvester_core.jobs.movie_materialize.run",
                           side_effect=lambda *a, **k: (calls.append("materialize"),
                                                        committers.append(k["committer"])) and
                           {"processed": 1, "counts": {"ok": 1}}), \
                mock.patch.object(bulk, "persist_preparation",
                                  return_value={"prepared": 1}):
            result = bulk.run(config, "lost-found", ["movie"], None)
        self.assertEqual(calls, ["scan", "materialize"])
        self.assertEqual(result["processed"], 1)
        self.assertFalse(committers[0].committing)

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
            self.assertFalse((config.movie_root / ".actors").exists())
            self.assertEqual(result["counts"]["applied"], 0)
            self.assertIn("Nothing has been written", result["message"])

    def test_missing_poster_reports_unresolved_target(self):
        config = mock.Mock()
        with mock.patch.object(bulk, "get_record", return_value={"local_target": "movie.nfo"}), \
                mock.patch("harvester_core.transport.transport_from_config", return_value=object()), \
                mock.patch("harvester_core.jobs.movie_materialize.run", return_value={
                    "processed": 1, "counts": {"poster_unresolved_target": 1}}), \
                mock.patch.object(bulk, "persist_preparation",
                                  return_value={"prepared": 0}):
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
        self.assertIn(".context-scroll", css)
        self.assertIn("overflow: auto", css)


if __name__ == "__main__":
    unittest.main()
