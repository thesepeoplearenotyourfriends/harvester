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
from harvester_core.storage import load_json, save_json_atomic


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
        nfo.write_text(
            "<movie><title>Localized</title><originaltitle>Original</originaltitle>"
            "<id>tt1234567</id></movie>"
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
        self.assertEqual(resolver_input["original_title"], "Original")
        self.assertEqual(resolver_input["imdb_id"], "tt1234567")

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
