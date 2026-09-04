import builtins
import inspect
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from harvester_core.config import load_config
from harvester_core.images import normalize_actor_image
from harvester_core.jobs.movie_actor_fetch import run as fetch_actors
from harvester_core.jobs.movie_actor_scan import (
    make_actor_work_queue,
    parse_nfo_file,
    resolve_actor_from_contexts,
    resolve_movie_tmdb_id,
)
from harvester_core.jobs.tv_materialize import (
    download_bytes,
    image_extension,
    run as materialize,
)
from harvester_core.jobs.tv_scan import build_nfo_payload, resolve_tvdb_series
from harvester_core.providers.tmdb import TMDBClient
from harvester_core.providers.tvdb import TVDBClient
from harvester_core.storage import load_json, save_json_atomic
from harvester_core.transport import parse_socks5, socks5_connect

ROOT = Path(__file__).resolve().parents[1]


class FakeProvider:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params or {}))
        value = self.responses[path]
        return value() if callable(value) else value


class HarvesterTests(unittest.TestCase):
    def config(self, base):
        return load_config(
            {"state_dir": "state", "movie_root": "Movie", "tv_root": "TV"},
            {},
            base,
        )

    def test_config_precedence_and_relative_paths_are_application_relative(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "keys_and_tokens.txt").write_text(
                "# comment\nTMDB_API_KEY=file\nTVDB_PIN=pin\nUNKNOWN=x\n"
            )
            config = load_config(
                {"tmdb_api_key": "explicit", "state_dir": "work"},
                {"TMDB_API_KEY": "environment"},
                base,
            )
            self.assertEqual(config.tmdb_api_key, "explicit")
            self.assertEqual(config.tvdb_pin, "pin")
            self.assertEqual(config.state_dir, (base / "work").resolve())

    def test_socks_config_environment_and_sanitized_parse_error(self):
        config = load_config({}, {
            "HARVESTER_SOCKS5": "proxy-user:proxy-pass@127.0.0.1:1080",
        })
        settings = parse_socks5(config.socks5)
        self.assertEqual((settings.host, settings.port), ("127.0.0.1", 1080))
        self.assertEqual((settings.username, settings.password),
                         ("proxy-user", "proxy-pass"))
        with self.assertRaises(ValueError) as caught:
            parse_socks5("user:DO_NOT_PRINT@missing-port")
        self.assertNotIn("DO_NOT_PRINT", str(caught.exception))

    def test_socks5_sends_destination_hostname_to_proxy(self):
        ready = threading.Event()
        destination = []

        def proxy(listener):
            ready.set()
            connection, _ = listener.accept()
            with connection:
                greeting = connection.recv(3)
                self.assertEqual(greeting, b"\x05\x01\x00")
                connection.sendall(b"\x05\x00")
                header = connection.recv(5)
                self.assertEqual(header[:4], b"\x05\x01\x00\x03")
                name = connection.recv(header[4]).decode("ascii")
                port = int.from_bytes(connection.recv(2), "big")
                destination.append((name, port))
                connection.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            thread = threading.Thread(target=proxy, args=(listener,))
            thread.start()
            ready.wait(1)
            settings = parse_socks5(f"127.0.0.1:{listener.getsockname()[1]}")
            stream = socks5_connect(settings, "api.themoviedb.org", 443, timeout=2)
            stream.close()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(destination, [("api.themoviedb.org", 443)])

    def test_atomic_json_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "state.json"
            save_json_atomic(path, {"done": 3})
            self.assertEqual(load_json(path), {"done": 3})
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_legacy_movie_nfo_variants_and_nested_actor(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "movie.nfo"
            path.write_text(
                "<movie><originaltitle>Legacy</originaltitle><releasedate>1999-01-02</releasedate>"
                "<id>tt0123</id><tmdbid>456</tmdbid><cast><actor><name>Actor A</name>"
                "<role>Lead</role><thumb>https://old/image.jpg</thumb></actor></cast></movie>"
            )
            record = parse_nfo_file(path)
            self.assertEqual(record["title"], "Legacy")
            self.assertEqual(record["year"], 1999)
            self.assertEqual(record["imdb_id"], "tt0123")
            self.assertEqual(record["tmdb_id"], 456)
            self.assertEqual(record["actors"][0]["role"], "Lead")

    def test_actor_queue_is_actor_centric_with_multiple_contexts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "movies"
            root.mkdir()
            for number in (1, 2):
                (root / f"{number}.nfo").write_text(
                    f"<movie><title>Film {number}</title><actor><name>Same Actor</name></actor></movie>"
                )
            queue = make_actor_work_queue([root], Path(temporary) / "queue.json")
            self.assertEqual(len(queue["actors"]["Same Actor"]["contexts"]), 2)

    def test_movie_title_year_resolution_without_ids(self):
        provider = FakeProvider({
            "/search/movie": {"results": [
                {"id": 7, "title": "Exact", "release_date": "2004-01-01", "popularity": 2},
                {"id": 8, "title": "Exact", "release_date": "1990-01-01"},
            ]}
        })
        result = resolve_movie_tmdb_id(
            provider, {"title": "Exact", "original_title": None, "year": 2004}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["movie_id"], 7)
        self.assertEqual(result["method"], "title_year_search")

    def test_actor_resolution_tries_multiple_movie_contexts(self):
        provider = FakeProvider({
            "/movie/1/credits": {"cast": []},
            "/movie/2/credits": {"cast": [
                {"id": 9, "name": "Actor", "order": 0, "profile_path": "/face.jpg"}
            ]},
        })
        contexts = [{"tmdb_id": 1}, {"tmdb_id": 2}]
        result = resolve_actor_from_contexts(
            provider, "Actor", contexts, "https://images/", "w185", 1
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["movie_tmdb_id"], 2)

    def test_rich_tv_payload_and_person_image_url(self):
        details = {
            "id": 10, "name": "Show", "year": "2020", "overview": "Plot",
            "remoteIds": [
                {"sourceName": "IMDB", "id": "tt10"},
                {"sourceName": "TheMovieDB.com", "id": "20"},
            ],
            "aliases": [{"name": "Alias"}], "originalCountry": "USA",
            "originalLanguage": "eng", "contentRatings": [{"country": "usa", "name": "TV-14"}],
            "genres": [{"name": "Drama"}], "companies": [{"name": "Studio"}],
            "tags": [{"name": "Mystery"}], "originalNetwork": {"name": "Network"},
            "characters": [{"personName": "Actor", "name": "Role", "sort": 1,
                            "personImgURL": "/people/face.jpg"}],
            "defaultSeasonType": 1,
            "seasons": [{"number": 1, "name": "One", "type": {"id": 1, "name": "Official"}}],
            "artworks": [{"image": "/poster.png", "width": 600, "height": 900}],
        }
        nfo, assets = build_nfo_payload(details)
        self.assertEqual(nfo["ids"]["imdb"], "tt10")
        self.assertEqual(nfo["network"], ["Network"])
        self.assertEqual(nfo["seasons"][0]["season_number"], 1)
        self.assertEqual(assets["actor_urls"][0]["url"], "https://artworks.thetvdb.com/people/face.jpg")
        self.assertTrue(assets["poster_url"].endswith("poster.png"))

    def test_ambiguous_tv_title_remains_ambiguous(self):
        provider = FakeProvider({"/search": ([
            {"id": 1, "name": "The Office", "year": "2001", "type": "series"},
            {"id": 2, "name": "The Office", "year": "2005", "type": "series"},
        ], False)})
        result = resolve_tvdb_series(provider, "The Office", None)
        self.assertEqual(result["status"], "ambiguous")

    def test_materializer_skips_existing_png_and_never_uses_tvdb(self):
        self.assertNotIn("providers", inspect.getsource(materialize))
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = self.config(base)
            show = config.tv_root / "Show"
            show.mkdir(parents=True)
            (show / "poster.png").write_bytes(b"png receipt")
            save_json_atomic(config.state_path("tv_show_urls_tvdb.json"), {
                "shows": {str(show): {"status": "matched", "folder_name": "Show",
                                      "nfo": {"title": "Show"},
                                      "assets": {"poster_url": "https://poster"}}}
            })
            materialize(config, overwrite_poster=False,
                        downloader=lambda url: self.fail("download called"))
            self.assertEqual((show / "poster.png").read_bytes(), b"png receipt")

    def test_materializer_downloads_actor_and_reports_without_printing(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = self.config(base)
            show = config.tv_root / "Show"
            show.mkdir(parents=True)
            save_json_atomic(config.state_path("tv_show_urls_tvdb.json"), {
                "shows": {str(show): {
                    "status": "matched", "folder_name": "Show",
                    "nfo": {"title": "Show"},
                    "assets": {"actor_urls": [
                        {"name": "Actor Name", "url": "https://face"}
                    ]},
                }}
            })
            events = []
            with patch("builtins.print") as terminal_print:
                result = materialize(
                    config,
                    reporter=events.append,
                    downloader=lambda url: (b"actor image", "image/jpeg"),
                    normalize=False,
                    sleep_between_requests=0,
                )
            terminal_print.assert_not_called()
            self.assertEqual(result["nfo_ok"], 1)
            self.assertEqual(result["actor_ok"], 1)
            nfo = (show / "show.nfo").read_text(encoding="utf-8")
            self.assertIn("<tvshow>", nfo)
            self.assertIn("<title>Show</title>", nfo)
            self.assertEqual(
                (config.tv_root / ".actors" / "Actor_Name.jpg").read_bytes(),
                b"actor image",
            )
            self.assertTrue(any(event.data.get("actor") == "Actor Name" for event in events))

    def test_png_detection_and_poster_retry_disabled(self):
        self.assertEqual(image_extension(b"\x89PNG\r\n\x1a\nrest", ""), ".png")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = self.config(base)
            show = config.tv_root / "Show"
            show.mkdir(parents=True)
            manifest = {"shows": {str(show): {
                "status": "matched", "folder_name": "Show", "nfo": {"title": "Show"},
                "assets": {"poster_url": "https://poster"},
                "materialize": {"poster": {"status": "error"}},
            }}}
            save_json_atomic(config.state_path("tv_show_urls_tvdb.json"), manifest)
            materialize(config, retry_failed=False,
                        downloader=lambda url: self.fail("download called"))
            saved = load_json(config.state_path("tv_show_urls_tvdb.json"))
            self.assertEqual(saved["shows"][str(show)]["materialize"]["poster"]["status"], "error")

    def test_image_retry_backoff_without_network_or_sleep(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"image"
        response.__enter__.return_value.headers.get.return_value = "image/jpeg"
        error = urllib.error.URLError("wobble")
        with patch("harvester_core.jobs.tv_materialize.urllib.request.urlopen",
                   side_effect=[error, response]), patch(
                       "harvester_core.jobs.tv_materialize.time.sleep") as sleep:
            self.assertEqual(download_bytes("https://image"), (b"image", "image/jpeg"))
            sleep.assert_called_once()

    def test_tvdb_refreshes_token_once_after_401(self):
        client = TVDBClient("secret")
        client.token = "old"
        unauthorized = urllib.error.HTTPError("secret-url", 401, "no", {}, None)
        with patch.object(client, "_request", side_effect=[unauthorized, {"data": {"id": 1}}]), \
             patch.object(client, "login", side_effect=lambda: setattr(client, "token", "new")) as login:
            data, _ = client.get("/series/1")
            self.assertEqual(data, {"id": 1})
            login.assert_called_once()

    def test_provider_404_is_cached_and_errors_hide_secrets(self):
        client = TMDBClient(api_key="VERY_SECRET")
        missing = urllib.error.HTTPError("https://x?api_key=VERY_SECRET", 404, "missing", {}, None)
        with patch("harvester_core.providers.tmdb.urllib.request.urlopen", side_effect=missing):
            self.assertEqual(client.get("/missing"), {})
        self.assertEqual(client.get("/missing"), {})
        denied = urllib.error.HTTPError("https://x?api_key=VERY_SECRET", 403, "denied", {}, None)
        with patch("harvester_core.providers.tmdb.urllib.request.urlopen", side_effect=denied):
            with self.assertRaises(RuntimeError) as caught:
                client.get("/denied")
        self.assertNotIn("VERY_SECRET", str(caught.exception))

        tmdb_body = io.BytesIO(json.dumps({
            "status_code": 7, "status_message": "Invalid API key"
        }).encode())
        tmdb_error = urllib.error.HTTPError(
            "https://x?api_key=VERY_SECRET", 401, "denied", {}, tmdb_body
        )
        with patch("harvester_core.providers.tmdb.urllib.request.urlopen",
                   side_effect=tmdb_error):
            with self.assertRaises(RuntimeError) as caught:
                client.get("/unauthorized")
        self.assertIn("HTTP 401: 7 Invalid API key", str(caught.exception))
        self.assertNotIn("VERY_SECRET", str(caught.exception))

        tvdb = TVDBClient("TVDB_SECRET")
        tvdb.token = "TOKEN_SECRET"
        tvdb_missing = urllib.error.HTTPError(
            "https://api/series/missing", 404, "missing", {}, None
        )
        with patch.object(tvdb, "_request", side_effect=tvdb_missing) as request:
            self.assertEqual(tvdb.get("/series/missing"), ({}, False))
            self.assertEqual(tvdb.get("/series/missing"), ({}, True))
            request.assert_called_once()

    def test_fetch_returns_compact_summary_and_skips_existing(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(Path(temporary))
            actor = config.movie_root / ".actors" / "A.jpg"
            actor.parent.mkdir(parents=True)
            actor.write_bytes(b"receipt")
            save_json_atomic(config.state_path("actor_thumb_urls_tmdb.json"),
                             {"A": ["https://invalid"]})
            result = fetch_actors(config, downloader=lambda _: self.fail("called"))
            self.assertNotIn("statuses", result)
            self.assertEqual(result["counts"]["exists"], 1)

    def test_pillow_absence_is_passthrough(self):
        real_import = builtins.__import__
        def missing(name, *args, **kwargs):
            if name == "PIL":
                raise ImportError
            return real_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=missing):
            self.assertEqual(normalize_actor_image(b"raw"), b"raw")

    def test_cli_help_status_and_missing_credentials_are_scoped(self):
        with tempfile.TemporaryDirectory() as temporary:
            for arguments in (["--help"], ["movies", "--help"], ["tv", "--help"],
                              ["--state-dir", temporary, "status"]):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "harvester.py"), *arguments],
                    cwd="/", capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(
            [sys.executable, str(ROOT / "harvester.py"), "movies", "scan-actors"],
            env={"PATH": os.environ["PATH"]}, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("TMDB capability unavailable", result.stderr)

    def test_status_marks_bad_json_unreadable_and_continues(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "movie_actor_queue.json").write_text("{broken")
            result = subprocess.run(
                [sys.executable, str(ROOT / "harvester.py"),
                 "--state-dir", str(state), "status"],
                cwd="/", capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("movie_actor_queue.json: unreadable", result.stdout)
            self.assertIn("tv_show_urls_tvdb.json: absent", result.stdout)


if __name__ == "__main__":
    unittest.main()
