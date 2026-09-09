import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from harvester_core.artifacts import (RecordingCommitter, apply_inbox_item,
                                      discard_inbox_item, get_inbox_item,
                                      list_inbox, persist_preparation)
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
        events = []
        result = materialize_tv(self.config, reporter=events.append,
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
        self.assertTrue(events)
        self.assertNotIn("artifact", {event.kind for event in events})
        self.assertIn("prepared", {event.kind for event in events})

    def test_tv_prepare_overlay_plans_shared_actor_only_once(self):
        shows = {}
        for name in ("One", "Two"):
            path = self.tv / name
            path.mkdir()
            shows[str(path)] = {"status": "matched", "folder_name": name,
                                "nfo": {"title": name}, "assets": {"actor_urls": [
                                    {"name": "Shared Actor", "url": "https://images/actor"}]}}
        save_json_atomic(self.config.state_path("tv_show_urls_tvdb.json"), {"shows": shows})
        recorder = RecordingCommitter()
        result = materialize_tv(self.config, write_nfo=False, write_poster=False,
                                downloader=lambda _url: (b"actor", "image/jpeg"),
                                normalize=False, sleep_between_requests=0,
                                committer=recorder)
        actor_writes = [action for action in result["planned"]
                        if action["action"] == "write" and
                        action["path"].endswith("Shared_Actor.jpg")]
        self.assertEqual(len(actor_writes), 1)

    def test_preparation_persists_manifest_and_blob_outside_library(self):
        recorder = RecordingCommitter()
        destination = self.movies / "Movie" / "movie.nfo"
        recorder.write(destination, b"prepared nfo")
        plan = persist_preparation(self.config, "lost-found", ["movie"], recorder)
        manifest_path = (self.root / ".cache" / "bulk" / "inbox" / plan["plan_id"] /
                         "manifest.json")
        manifest = json.loads(manifest_path.read_text())
        action = manifest["actions"][0]
        self.assertNotIn("bytes", action)
        self.assertEqual((manifest_path.parent / action["blob"]).read_bytes(),
                         b"prepared nfo")
        self.assertFalse(destination.exists())

    def test_inbox_survives_reload_and_opening_marks_seen_without_deciding(self):
        recorder = RecordingCommitter()
        recorder.write(self.movies / "Movie" / "movie.nfo", b"offline")
        plan = persist_preparation(self.config, "lost-found", ["movie"], recorder,
                                   display_title="Movie")
        self.assertEqual(len(list_inbox(self.config)), 1)
        reopened = load_config({"state_dir": self.root / "state",
                                "movie_root": self.movies, "tv_root": self.tv},
                               environ={}, app_dir=self.root)
        item = get_inbox_item(reopened, plan["plan_id"], mark_seen=True)
        self.assertTrue(item["seen"])
        self.assertEqual(item["state"], "ready")
        self.assertEqual(len(list_inbox(reopened)), 1)

    def test_grouped_identities_are_one_review_item(self):
        recorder = RecordingCommitter()
        recorder.write(self.movies / "Shared" / "poster.jpg", b"poster")
        persist_preparation(self.config, "missing-posters", ["first.nfo", "second.nfo"],
                            recorder, display_title="Shared")
        items = list_inbox(self.config)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["identities"], ["first.nfo", "second.nfo"])

    def test_apply_is_offline_and_discard_never_mutates_media(self):
        target = self.movies / "Movie" / "movie.nfo"
        target.parent.mkdir()
        recorder = RecordingCommitter(); recorder.write(target, b"frozen")
        plan = persist_preparation(self.config, "lost-found", ["movie"], recorder)
        apply_inbox_item(self.config, plan["plan_id"])
        self.assertEqual(target.read_bytes(), b"frozen")
        self.assertEqual(list_inbox(self.config), [])

        untouched = self.movies / "Other" / "movie.nfo"
        recorder = RecordingCommitter(); recorder.write(untouched, b"discarded")
        plan = persist_preparation(self.config, "lost-found", ["other"], recorder)
        discard_inbox_item(self.config, plan["plan_id"])
        self.assertFalse(untouched.exists())

    def test_newly_applied_nfo_is_readable_by_library_users(self):
        target = self.movies / "Movie" / "movie.nfo"
        recorder = RecordingCommitter(); recorder.write(target, b"<movie/>")
        plan = persist_preparation(self.config, "lost-found", [str(target)], recorder)
        old_umask = os.umask(0o022)
        try:
            apply_inbox_item(self.config, plan["plan_id"])
        finally:
            os.umask(old_umask)
        self.assertTrue(stat.S_IMODE(target.stat().st_mode) & stat.S_IROTH)

    def test_stale_destination_blocks_apply_and_marks_attention(self):
        target = self.movies / "Movie" / "movie.nfo"; target.parent.mkdir()
        recorder = RecordingCommitter(); recorder.write(target, b"prepared")
        plan = persist_preparation(self.config, "lost-found", ["movie"], recorder)
        target.write_bytes(b"newer local work")
        with self.assertRaisesRegex(ValueError, "filesystem changed"):
            apply_inbox_item(self.config, plan["plan_id"])
        self.assertEqual(target.read_bytes(), b"newer local work")
        self.assertEqual(get_inbox_item(self.config, plan["plan_id"])["state"],
                         "needs_attention")

    def test_apply_rejects_escape_and_tampered_blob(self):
        outside = self.root / "outside"
        recorder = RecordingCommitter(); recorder.write(outside, b"escape")
        plan = persist_preparation(self.config, "lost-found", ["escape"], recorder)
        with self.assertRaisesRegex(ValueError, "outside configured"):
            apply_inbox_item(self.config, plan["plan_id"])
        self.assertFalse(outside.exists())

        target = self.movies / "Movie" / "poster.jpg"; target.parent.mkdir(exist_ok=True)
        recorder = RecordingCommitter(); recorder.write(target, b"poster")
        plan = persist_preparation(self.config, "missing-posters", ["poster"], recorder)
        manifest = get_inbox_item(self.config, plan["plan_id"])
        blob = self.root / ".cache" / "bulk" / "inbox" / plan["plan_id"] / manifest["actions"][0]["blob"]
        blob.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "size/hash"):
            apply_inbox_item(self.config, plan["plan_id"])
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
