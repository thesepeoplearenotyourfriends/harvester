import json
import tempfile
import unittest
from pathlib import Path

from harvester_core.artifacts import RecordingCommitter
from harvester_core.config import load_config
from harvester_core.jobs.movie_actor_fetch import run as fetch_actors
from harvester_core.jobs.movie_materialize import run as materialize_movies
from harvester_core.jobs.tv_materialize import run as materialize_tv
from harvester_core.storage import save_json_atomic


class ArtifactCommitSeamTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.movies = self.root / "movies"
        self.tv = self.root / "tv"
        self.movies.mkdir()
        self.tv.mkdir()
        self.config = load_config({"state_dir": self.root / "state",
                                   "movie_root": self.movies, "tv_root": self.tv},
                                  environ={}, app_dir=self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_actor_prepare_records_bytes_without_artifact_or_receipt(self):
        save_json_atomic(self.config.state_path("actor_thumb_urls_tmdb.json"),
                         {"Actor": ["https://images/actor"]})
        recorder = RecordingCommitter()
        result = fetch_actors(self.config, downloader=lambda _url: (b"actor", "image/jpeg"),
                              normalize=False, committer=recorder)
        self.assertFalse((self.movies / ".actors").exists())
        self.assertFalse(self.config.state_path("actor_photo_download_status.json").exists())
        writes = [action for action in result["planned"] if action["action"] == "write"]
        self.assertEqual(writes, [{"action": "write",
                                  "path": str(self.movies / ".actors" / "Actor.jpg"),
                                  "bytes": b"actor"}])

    def test_movie_prepare_leaves_artifacts_and_manifest_unchanged(self):
        folder = self.movies / "Movie"
        folder.mkdir()
        nfo = folder / "movie.nfo"
        poster = folder / "poster.jpg"
        manifest_path = self.config.state_path("movie_manifest_tmdb.json")
        save_json_atomic(manifest_path, {"movies": {str(nfo): {
            "status": "ok", "nfo_path": str(nfo), "poster_path": str(poster),
            "poster_url": "https://images/poster", "nfo": {"title": "Movie"}}}})
        before = manifest_path.read_bytes()
        recorder = RecordingCommitter()
        result = materialize_movies(self.config, downloader=lambda _url: (b"poster", "image/jpeg"),
                                    committer=recorder)
        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertFalse(nfo.exists())
        self.assertFalse(poster.exists())
        self.assertEqual({Path(action["path"]).name for action in result["planned"]
                          if action["action"] == "write"}, {"movie.nfo", "poster.jpg"})

    def test_tv_prepare_includes_all_artifacts_without_mutating_state(self):
        show = self.tv / "Show"
        show.mkdir()
        manifest_path = self.config.state_path("tv_show_urls_tvdb.json")
        save_json_atomic(manifest_path, {"shows": {str(show): {
            "status": "matched", "folder_name": "Show", "nfo": {"title": "Show"},
            "assets": {"poster_url": "https://images/poster", "actor_urls": [
                {"name": "Actor", "url": "https://images/actor"}]}}}})
        before = manifest_path.read_bytes()
        recorder = RecordingCommitter()
        result = materialize_tv(self.config,
                                downloader=lambda _url: (b"\xff\xd8image", "image/jpeg"),
                                normalize=False, sleep_between_requests=0,
                                committer=recorder)
        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertFalse((show / "show.nfo").exists())
        self.assertFalse((show / "poster.jpg").exists())
        self.assertFalse((self.tv / ".actors").exists())
        self.assertEqual({Path(action["path"]).name for action in result["planned"]
                          if action["action"] == "write"},
                         {"show.nfo", "poster.jpg", "Actor.jpg"})


if __name__ == "__main__":
    unittest.main()
