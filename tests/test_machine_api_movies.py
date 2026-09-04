import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from harvester_core.config import load_config
from harvester_core.jobs.movie_materialize import run as materialize
from harvester_core.jobs.movie_scan import run as scan
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


if __name__ == "__main__":
    unittest.main()
