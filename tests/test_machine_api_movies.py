import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from harvester_core.config import load_config
from harvester_core.jobs.movie_materialize import run as materialize
from harvester_core.jobs.movie_scan import run as scan
from harvester_core.jobs.movie_scan import discover_movies
from harvester_core.jobs.tv_materialize import run as materialize_tv
from harvester_core.providers.profiles import profiles
from harvester_core.rescan import rescan
from harvester_core.storage import load_json, save_json_atomic
from harvester_core.api import inspect_item, list_artifacts


ROOT = Path(__file__).resolve().parent.parent


class MovieProvider:
    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(path)
        if path == "/configuration":
            return {"images": {"secure_base_url": "https://images/", "poster_sizes": ["w780"]}}
        if path == "/movie/7":
            return {"id": 7, "title": "Example", "original_title": "Example Original", "release_date": "2020-01-02", "overview": "Plot", "poster_path": "/poster.jpg", "genres": [{"name": "Drama"}]}
        if path == "/movie/7/credits":
            return {"cast": [{"name": "Actor", "character": "Role", "order": 0}], "crew": [{"name": "Director", "job": "Director"}]}
        raise AssertionError(path)


class Response:
    headers = {"Content-Type": "image/jpeg"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b"\xff\xd8poster"


class RecordingTransport:
    def __init__(self):
        self.user_agent = None

    def open(self, request, timeout=None):
        self.user_agent = request.get_header("User-agent")
        return Response()


class MachineApiMovieTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.movies = base / "movies"
        self.state = base / "state"
        folder = self.movies / "Example (2020)"
        folder.mkdir(parents=True)
        (folder / "movie.nfo").write_text("<movie><title>Example</title><year>2020</year><uniqueid type='tmdb'>7</uniqueid></movie>")
        self.config = load_config({"movie_root": self.movies, "tv_root": base / "tv", "state_dir": self.state}, environ={}, app_dir=ROOT)

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "harvester.py"), "--state-dir", str(self.state), "--movie-root", str(self.movies), "--tv-root", str(Path(self.temp.name) / "tv"), *args], text=True, capture_output=True)

    def test_help_and_provider_output_contract(self):
        help_result = self.cli("--help")
        self.assertIn("api", help_result.stdout)
        self.assertIn("status", help_result.stdout)
        result = self.cli("api", "providers")
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(result.returncode, 0)
        self.assertEqual([item["type"] for item in records], ["result"])
        serialized = result.stdout.casefold()
        self.assertNotIn("secret", serialized)
        self.assertEqual({x["key"] for x in records[0]["result"]["providers"]}, {"tmdb", "tvdb"})

    def test_api_errors_are_json_and_nonzero(self):
        result = self.cli("api", "get", "actor", "Missing")
        record = json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(record["type"], "error")
        self.assertFalse(record["ok"])
        self.assertEqual(result.stderr, "")

    def test_scan_freezes_and_materialize_is_provider_free(self):
        provider = MovieProvider()
        result = scan(self.config, provider)
        self.assertEqual(result["processed"], 1)
        manifest = load_json(self.state / "movie_manifest_tmdb.json")
        record = next(iter(manifest["movies"].values()))
        self.assertEqual(record["tmdb_id"], 7)
        self.assertEqual(record["poster_url"], "https://images/w780/poster.jpg")
        self.assertNotIn("poster_url", record["nfo"])
        self.assertIsNone(record["poster_path"])
        self.assertEqual(record["poster_target_status"], "unresolved")

        # Materialization receives an explicit local poster target; discovery
        # does not invent one for an arbitrary NFO.
        record["poster_path"] = str(Path(record["nfo_path"]).parent / "chosen-poster.jpg")
        record["poster_target_status"] = "resolved"
        save_json_atomic(self.state / "movie_manifest_tmdb.json", manifest)

        old = Path(record["nfo_path"]).read_bytes()
        materialize(self.config, downloader=lambda url: (b"\xff\xd8poster", "image/jpeg"))
        self.assertEqual(Path(record["nfo_path"]).read_bytes(), old)
        self.assertTrue(Path(record["poster_path"]).is_file())

        materialize(self.config, overwrite_nfo=True, overwrite_poster=True,
                    downloader=lambda url: (b"\xff\xd8new", "image/jpeg"))
        self.assertIn(b"<actor>", Path(record["nfo_path"]).read_bytes())
        self.assertEqual(Path(record["poster_path"]).read_bytes(), b"\xff\xd8new")

    def test_offline_list_and_inventory_read_shared_manifest(self):
        save_json_atomic(self.state / "movie_manifest_tmdb.json", {"movies": {"/one.nfo": {"status": "unresolved", "nfo_path": "/one.nfo", "poster_path": "/one-poster.jpg"}}})
        for command in (("api", "list", "movies"), ("api", "inventory")):
            result = self.cli(*command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["type"], "result")

    def test_artifact_inspection_reads_current_files_not_frozen_work_fields(self):
        folder = self.movies / "Example (2020)"
        nfo = folder / "movie.nfo"
        poster = folder / "poster.png"
        video = folder / "Example.mkv"
        poster.write_bytes(b"png")
        video.write_bytes(b"video")
        save_json_atomic(self.state / "movie_manifest_tmdb.json", {"movies": {
            str(nfo): {"status": "failed", "title": "Stale title", "tries": 99,
                       "nfo_path": str(nfo), "poster_target_status": "unresolved"}
        }})

        before = (self.state / "movie_manifest_tmdb.json").read_bytes()
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
            item = inspect_item(self.config, "movie", str(nfo))

        self.assertEqual(item["nfo"]["fields"]["title"], "Example")
        self.assertEqual(item["nfo"]["fields"]["unique_ids"], {"tmdb": "7"})
        self.assertEqual(item["poster"], {"present": True, "path": str(poster),
                                           "candidates": [str(poster)]})
        self.assertEqual(item["video_files"], [str(video)])
        self.assertNotIn("tries", item)
        self.assertNotIn("poster_target_status", item)
        self.assertEqual((self.state / "movie_manifest_tmdb.json").read_bytes(), before)

    def test_missing_poster_projection_groups_shared_directory_and_marks_ambiguity(self):
        folder = self.movies / "Shared"
        folder.mkdir()
        first = folder / "first.nfo"
        second = folder / "second.nfo"
        first.write_text("<movie><title>First</title></movie>")
        second.write_text("<movie><title>Second</title></movie>")
        save_json_atomic(self.state / "movie_manifest_tmdb.json", {"movies": {
            str(first): {"nfo_path": str(first)},
            str(second): {"nfo_path": str(second)},
        }})

        items = list_artifacts(self.config, "movie", missing="poster",
                               group_directories=True)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["manifest_identities"], [str(first), str(second)])
        self.assertEqual(items[0]["ownership"], "ambiguous")
        self.assertTrue(items[0]["nfo_present"])
        self.assertNotIn("nfo", items[0])
        self.assertNotIn("video_files", items[0])

    def test_state_workflow_projection_preserves_matching_movie_identity(self):
        folder = self.movies / "Mixed"
        folder.mkdir()
        ok = folder / "ok.nfo"
        failed = folder / "failed.nfo"
        ok.write_text("<movie><title>Okay</title></movie>")
        failed.write_text("<movie><title>Failed</title></movie>")
        save_json_atomic(self.state / "movie_manifest_tmdb.json", {"movies": {
            str(ok): {"status": "ok", "title": "Okay", "nfo_path": str(ok)},
            str(failed): {"status": "failed", "title": "Failed", "nfo_path": str(failed)},
        }})

        items = list_artifacts(self.config, "movie", status="failed")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["identifier"], str(failed))
        self.assertEqual(items[0]["manifest_identities"], [str(failed)])
        detail = inspect_item(self.config, "movie", items[0]["identifier"])
        self.assertEqual(detail["selected_manifest_identity"], str(failed))
        self.assertEqual(detail["label"], "Failed")

    def test_nonstandard_poster_candidate_prevents_missing_directory(self):
        folder = self.movies / "Shared Poster"
        folder.mkdir()
        first = folder / "first.nfo"
        second = folder / "second.nfo"
        first.write_text("<movie><title>First</title></movie>")
        second.write_text("<movie><title>Second</title></movie>")
        candidate = folder / "First Movie-poster.jpg"
        candidate.write_bytes(b"poster")
        save_json_atomic(self.state / "movie_manifest_tmdb.json", {"movies": {
            str(first): {"nfo_path": str(first)},
            str(second): {"nfo_path": str(second)},
        }})

        detail = inspect_item(self.config, "movie", str(folder))

        self.assertEqual(detail["ownership"]["status"], "ambiguous")
        self.assertEqual(detail["poster"]["candidates"], [str(candidate)])
        self.assertEqual(list_artifacts(self.config, "movie", missing="poster",
                                        group_directories=True), [])

    def test_brief_lists_and_offline_search_return_compact_typed_records(self):
        save_json_atomic(self.state / "movie_actor_queue.json", {"actors": {
            "Aaron Paul": {"status": "ok", "contexts": ["large", "detail"],
                           "tmdb": {"frozen": True}}
        }})
        save_json_atomic(self.state / "movie_manifest_tmdb.json", {"movies": {
            "/one.nfo": {"status": "unresolved", "title": "Aaron's Movie",
                          "nfo_path": "/one.nfo", "poster_url": "https://remote"}
        }})
        listed = json.loads(self.cli("api", "list", "actors", "--brief", "--missing", "image").stdout)["result"]["items"]
        self.assertEqual(listed, [{"kind": "actor", "name": "Aaron Paul", "status": "ok", "local_file": False}])
        self.assertNotIn("contexts", listed[0])
        with mock.patch("harvester_core.providers.tmdb.TMDBClient", side_effect=AssertionError("network adapter constructed")):
            result = self.cli("api", "search", "aaron", "--limit", "1")
        found = json.loads(result.stdout)["result"]["items"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "actor")
        self.assertLessEqual(len(found), 1)

    def test_failed_movie_queue_matches_combined_inventory_count(self):
        save_json_atomic(self.state / "movie_manifest_tmdb.json", {"movies": {
            "/error.nfo": {"status": "error", "nfo_path": "/error.nfo"},
            "/failed.nfo": {"status": "failed", "nfo_path": "/failed.nfo"},
            "/ok.nfo": {"status": "ok", "nfo_path": "/ok.nfo"},
        }})
        inventory = json.loads(self.cli("api", "inventory").stdout)["result"]
        queue = json.loads(self.cli(
            "api", "list", "movies", "--brief", "--status", "failed",
        ).stdout)["result"]["items"]
        self.assertEqual(inventory["movies"]["failed"], 2)
        self.assertEqual(len(queue), inventory["movies"]["failed"])
        self.assertEqual({item["status"] for item in queue}, {"error", "failed"})

    def test_offline_rescan_rebuilds_censuses_and_preserves_known_provider_state(self):
        movie_nfo = self.movies / "Example (2020)" / "movie.nfo"
        movie_nfo.write_text("""<movie><title>Example Changed</title><year>2020</year>
            <actor><name>Known Actor</name><role>Lead</role></actor></movie>""")
        (self.movies / ".actors").mkdir()
        (self.movies / ".actors" / "Known_Actor.jpg").write_bytes(b"jpeg")
        show = Path(self.temp.name) / "tv" / "Known Show (2021)"
        show.mkdir(parents=True)
        save_json_atomic(self.state / "movie_actor_queue.json", {"_meta": {
            "created": "actor-created", "image_size": "w342",
            "max_images_per_actor": 3, "last_run": {"processed": 2},
        }, "actors": {
            "Known Actor": {"status": "ok", "tmdb_person_id": 42,
                            "urls": ["https://known"], "contexts": []},
            "Removed Actor": {"status": "ok", "urls": ["https://removed"], "contexts": []},
        }})
        save_json_atomic(self.state / "actor_thumb_urls_tmdb.json", {
            "Known Actor": ["https://known"], "Removed Actor": ["https://removed"],
        })
        target = str(movie_nfo.resolve())
        save_json_atomic(self.state / "movie_manifest_tmdb.json", {"_meta": {
            "created": "movie-created", "last_materialize": {"written": 4},
        }, "movies": {
            target: {"status": "ok", "tmdb_id": 7, "nfo_path": target},
            "/removed.nfo": {"status": "error", "nfo_path": "/removed.nfo"},
        }})
        save_json_atomic(self.state / "tv_show_urls_tvdb.json", {"_meta": {
            "created": "tv-created", "last_run": {"matched": 8},
            "last_materialize_run": {"written": 6},
        }, "shows": {
            str(show.resolve()): {"status": "matched", "tvdb_id": 9},
            "/removed-show": {"status": "not_found"},
        }})

        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
            result = rescan(self.config)

        actors = load_json(self.state / "movie_actor_queue.json")["actors"]
        movies = load_json(self.state / "movie_manifest_tmdb.json")["movies"]
        shows = load_json(self.state / "tv_show_urls_tvdb.json")["shows"]
        actor_state = load_json(self.state / "movie_actor_queue.json")
        movie_state = load_json(self.state / "movie_manifest_tmdb.json")
        show_state = load_json(self.state / "tv_show_urls_tvdb.json")
        actor_urls = load_json(self.state / "actor_thumb_urls_tmdb.json")
        self.assertEqual(set(actors), {"Known Actor"})
        self.assertEqual(actors["Known Actor"]["tmdb_person_id"], 42)
        self.assertEqual(actors["Known Actor"]["contexts"][0]["title"], "Example Changed")
        self.assertEqual(result["inventory"]["actors"]["local"], 1)
        self.assertEqual(actor_state["_meta"]["created"], "actor-created")
        self.assertEqual(actor_state["_meta"]["image_size"], "w342")
        self.assertEqual(actor_state["_meta"]["max_images_per_actor"], 3)
        self.assertEqual(actor_state["_meta"]["last_run"], {"processed": 2})
        self.assertEqual(actor_urls, {"Known Actor": ["https://known"]})
        self.assertEqual(set(movies), {target})
        self.assertEqual(movies[target]["tmdb_id"], 7)
        self.assertEqual(movies[target]["title"], "Example Changed")
        self.assertEqual(movie_state["_meta"]["created"], "movie-created")
        self.assertEqual(movie_state["_meta"]["last_materialize"], {"written": 4})
        self.assertEqual(set(shows), {str(show.resolve())})
        self.assertEqual(shows[str(show.resolve())]["tvdb_id"], 9)
        self.assertEqual(show_state["_meta"]["created"], "tv-created")
        self.assertEqual(show_state["_meta"]["last_run"], {"matched": 8})
        self.assertEqual(show_state["_meta"]["last_materialize_run"], {"written": 6})

    def test_rescan_missing_roots_leave_existing_state_unchanged(self):
        files = {
            "movie_actor_queue.json": {"_meta": {"history": "actor"}, "actors": {"Old": {}}},
            "actor_thumb_urls_tmdb.json": {"Old": ["https://old"]},
            "movie_manifest_tmdb.json": {"_meta": {"history": "movie"}, "movies": {"old": {}}},
            "tv_show_urls_tvdb.json": {"_meta": {"history": "tv"}, "shows": {"old": {}}},
        }
        for name, value in files.items():
            save_json_atomic(self.state / name, value)
        before = {name: (self.state / name).read_bytes() for name in files}

        missing_movie = load_config({"state_dir": self.state,
                                     "movie_root": Path(self.temp.name) / "missing-movies",
                                     "tv_root": Path(self.temp.name) / "tv"},
                                    environ={}, app_dir=ROOT)
        with self.assertRaisesRegex(FileNotFoundError, "MOVIE_ROOT"):
            rescan(missing_movie)
        self.assertEqual(before, {name: (self.state / name).read_bytes() for name in files})

        missing_tv = load_config({"state_dir": self.state, "movie_root": self.movies,
                                  "tv_root": Path(self.temp.name) / "missing-tv"},
                                 environ={}, app_dir=ROOT)
        with self.assertRaisesRegex(FileNotFoundError, "TV_ROOT"):
            rescan(missing_tv)
        self.assertEqual(before, {name: (self.state / name).read_bytes() for name in files})

    def test_api_rescan_is_whole_library_only(self):
        (Path(self.temp.name) / "tv").mkdir(exist_ok=True)
        result = self.cli("api", "rescan")
        self.assertEqual(result.returncode, 0, result.stdout)
        payload = json.loads(result.stdout)["result"]
        self.assertEqual(set(payload["rescanned"]), {"actors", "movies", "shows"})
        self.assertTrue(load_json(self.state / "movie_manifest_tmdb.json")["_meta"]["created"])
        rejected = self.cli("api", "rescan", "actors")
        self.assertNotEqual(rejected.returncode, 0)

    def test_rescan_readable_empty_roots_leave_all_state_unchanged(self):
        empty_movies = Path(self.temp.name) / "empty-movies"
        empty_tv = Path(self.temp.name) / "empty-tv"
        empty_movies.mkdir()
        empty_tv.mkdir()
        files = {
            "movie_actor_queue.json": {"actors": {"Existing Actor": {}}},
            "actor_thumb_urls_tmdb.json": {"Existing Actor": ["https://old"]},
            "movie_manifest_tmdb.json": {"movies": {"existing": {}}},
            "tv_show_urls_tvdb.json": {"shows": {"existing": {}}},
        }
        for name, value in files.items():
            save_json_atomic(self.state / name, value)
        before = {name: (self.state / name).read_bytes() for name in files}
        config = load_config({"state_dir": self.state, "movie_root": empty_movies,
                              "tv_root": empty_tv}, environ={}, app_dir=ROOT)

        with self.assertRaisesRegex(RuntimeError, "empty discovery"):
            rescan(config)

        self.assertEqual(before, {name: (self.state / name).read_bytes() for name in files})

    def test_tv_receipt_uses_materializer_filename(self):
        show = Path(self.temp.name) / "tv" / "Example"
        show.mkdir(parents=True)
        (show / "show.nfo").write_text("<tvshow/>")
        save_json_atomic(self.state / "tv_show_urls_tvdb.json", {
            "shows": {str(show): {"status": "matched"}}
        })
        result = self.cli("api", "get", "show", str(show))
        self.assertTrue(json.loads(result.stdout)["result"]["local_receipts"]["nfo"])

    def test_lone_video_supplies_nfo_target_but_not_poster_target(self):
        folder = self.movies / "Needs Metadata (2024)"
        folder.mkdir()
        video = folder / "Needs Metadata.mkv"
        video.write_bytes(b"video")
        record = discover_movies(self.movies)[str(video.with_suffix(".nfo").resolve())]
        self.assertEqual(record["nfo_path"], str(video.with_suffix(".nfo").resolve()))
        self.assertIsNone(record["poster_path"])
        self.assertEqual(record["poster_target_status"], "unresolved")

        (folder / "part-two.mp4").write_bytes(b"video")
        self.assertNotIn(str(video.with_suffix(".nfo").resolve()), discover_movies(self.movies))

    def test_movie_discovery_preserves_legacy_identity_fields(self):
        folder = self.movies / "Identity"
        folder.mkdir()
        nfo = folder / "identity.nfo"
        nfo.write_text("<movie><title>Wrong</title><year>1900</year></movie>")
        with mock.patch(
            "harvester_core.jobs.movie_scan.resolve_movie_tmdb_id",
            return_value={"ok": False, "reason": "test"},
        ):
            scan(self.config, MovieProvider(), targets=[str(nfo.resolve())])

        nfo.write_text(
            "<movie><title>Localized</title><originaltitle>Original</originaltitle>"
            "<year>2001</year><id>tt1234567</id></movie>"
        )
        record = discover_movies(self.movies)[str(nfo.resolve())]
        self.assertEqual(record["title"], "Localized")
        self.assertEqual(record["original_title"], "Original")
        self.assertEqual(record["imdb_id"], "tt1234567")
        with mock.patch(
            "harvester_core.jobs.movie_scan.resolve_movie_tmdb_id",
            return_value={"ok": False, "reason": "test"},
        ) as resolve:
            scan(self.config, MovieProvider(), targets=[str(nfo.resolve())])
        resolver_input = resolve.call_args.args[1]
        self.assertEqual(resolver_input["title"], "Localized")
        self.assertEqual(resolver_input["original_title"], "Original")
        self.assertEqual(resolver_input["year"], 2001)
        self.assertEqual(resolver_input["imdb_id"], "tt1234567")

        saved = load_json(self.state / "movie_manifest_tmdb.json")["movies"][str(nfo.resolve())]
        self.assertEqual(saved["tries"], 2)

    def test_one_poster_is_not_assigned_to_multiple_nfos(self):
        folder = self.movies / "Anthology"
        folder.mkdir()
        first = folder / "first.nfo"
        second = folder / "second.nfo"
        first.write_text("<movie><title>First</title></movie>")
        second.write_text("<movie><title>Second</title></movie>")
        (folder / "poster.jpg").write_bytes(b"poster")
        records = discover_movies(self.movies)
        self.assertIsNone(records[str(first.resolve())]["poster_path"])
        self.assertIsNone(records[str(second.resolve())]["poster_path"])

    def test_tvdb_profile_advertises_person_images(self):
        tvdb = next(item for item in profiles(self.config) if item["key"] == "tvdb")
        self.assertIn("person.image", tvdb["capabilities"])

    def test_tv_materialization_artifact_flags_do_not_touch_siblings(self):
        show = Path(self.temp.name) / "tv" / "Scoped"
        show.mkdir(parents=True)
        manifest = {"shows": {str(show): {
            "status": "matched", "folder_name": "Scoped",
            "nfo": {"title": "Scoped"},
            "assets": {"poster_url": "https://poster", "actor_urls": [
                {"name": "Actor", "url": "https://actor"}
            ]},
        }}}
        save_json_atomic(self.state / "tv_show_urls_tvdb.json", manifest)
        materialize_tv(
            self.config, write_nfo=True, write_poster=False, write_actors=False,
            downloader=lambda url: self.fail("download called"),
        )
        actors_dir = Path(self.temp.name) / "tv" / ".actors"
        self.assertFalse(actors_dir.exists())
        self.assertTrue((show / "show.nfo").exists())
        scoped_state = load_json(
            self.state / "tv_show_urls_tvdb.json"
        )["shows"][str(show)]["materialize"]
        self.assertIn("nfo", scoped_state)
        self.assertNotIn("poster", scoped_state)
        self.assertNotIn("actors", scoped_state)
        self.assertNotIn("status", scoped_state)
        (show / "show.nfo").unlink()

        materialize_tv(
            self.config, write_nfo=False, write_poster=False, write_actors=True,
            downloader=lambda url: (b"image", "image/jpeg"),
            sleep_between_requests=0,
        )
        self.assertFalse((show / "show.nfo").exists())
        self.assertFalse((show / "poster.jpg").exists())
        self.assertTrue((actors_dir / "Actor.jpg").exists())

    def test_actor_image_refresh_does_not_construct_tmdb_client(self):
        import harvester
        args = harvester.parser().parse_args([
            "--state-dir", str(self.state), "--movie-root", str(self.movies),
            "api", "refresh", "actor", "Actor", "--aspect", "image",
        ])
        with mock.patch("harvester_core.jobs.movie_actor_fetch.run", return_value={"processed": 1}) as fetch:
            with mock.patch("harvester_core.providers.tmdb.TMDBClient", side_effect=AssertionError("TMDB used")):
                with mock.patch("sys.stdout"):
                    self.assertEqual(harvester.api_main(args, self.config), 0)
        fetch.assert_called_once()

    def test_movie_download_identity_and_stale_cleanup_failure(self):
        target = self.movies / "Example (2020)" / "selected.jpg"
        stale = target.with_suffix(".png")
        stale.write_bytes(b"old")
        save_json_atomic(self.state / "movie_manifest_tmdb.json", {"movies": {
            "movie": {"status": "ok", "nfo_path": str(target.with_suffix(".nfo")),
                      "poster_path": str(target), "poster_url": "https://images/poster",
                      "nfo": {"title": "Example"}}
        }})
        transport = RecordingTransport()
        original_unlink = Path.unlink

        def fail_stale(path, *args, **kwargs):
            if path == stale:
                raise PermissionError("busy")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_stale):
            materialize(self.config, overwrite_poster=True, write_nfo=False,
                        transport=transport)
        record = load_json(self.state / "movie_manifest_tmdb.json")["movies"]["movie"]
        self.assertEqual(transport.user_agent, "local-tmdb-movie-materializer/1.0")
        self.assertEqual(record["materialize"]["poster"]["status"], "ok")
        self.assertIn("cleanup_error", record["materialize"]["poster"])
        self.assertEqual(target.read_bytes(), b"\xff\xd8poster")


if __name__ == "__main__":
    unittest.main()
