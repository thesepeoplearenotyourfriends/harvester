import builtins, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from harvester_core.config import load_config
from harvester_core.storage import load_json, save_json_atomic
from harvester_core.jobs.tv_scan import resolve
from harvester_core.jobs.tv_materialize import run as materialize
from harvester_core.jobs.movie_actor_fetch import run as fetch
from harvester_core.jobs.movie_actor_scan import run as movie_scan
from harvester_core.images import normalize_actor_image

ROOT=Path(__file__).resolve().parents[1]

class FakeTVDB:
    def get(self,path,params):
        return ([{"id":1,"name":"The Office","year":"2001"},{"id":2,"name":"The Office","year":"2005"}],False)

class Tests(unittest.TestCase):
    def test_config_precedence(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td,"keys_and_tokens.txt").write_text("# hi\nTMDB_API_KEY=file\nTVDB_PIN=p\nBAD=x\n")
            cfg=load_config({"tmdb_api_key":"explicit"},{"TMDB_API_KEY":"env"},Path(td))
            self.assertEqual(cfg.tmdb_api_key,"explicit"); self.assertEqual(cfg.tvdb_pin,"p")
    def test_atomic_json_and_resume(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td,"nested","state.json"); save_json_atomic(path,{"done":3})
            self.assertEqual(load_json(path,{})["done"],3); self.assertFalse(list(path.parent.glob("*.tmp")))
    def test_ambiguous_tv_title_stays_ambiguous(self):
        selected,status,_=resolve(FakeTVDB(),"The Office",None)
        self.assertIsNone(selected); self.assertEqual(status,"ambiguous")
    def test_materialize_uses_frozen_data_and_skips(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td); show=base/"TV"/"Show"; show.mkdir(parents=True); (show/"poster.jpg").write_bytes(b"old")
            cfg=load_config({"state_dir":base/"state","tv_root":base/"TV","movie_root":base/"Movie"},{},base)
            save_json_atomic(cfg.state_path("tv_show_urls_tvdb.json"),{"shows":{str(show):{"status":"matched","nfo":{"title":"Show"},"assets":{}}}})
            result=materialize(cfg,overwrite_nfo=False,overwrite_poster=False)
            self.assertEqual(result["processed"],1); self.assertEqual((show/"poster.jpg").read_bytes(),b"old")
    def test_movie_existing_artifact_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td); cfg=load_config({"state_dir":base/"state","movie_root":base/"Movie","tv_root":base/"TV"},{},base)
            actor=cfg.movie_root/".actors"/"A.jpg"; actor.parent.mkdir(parents=True); actor.write_bytes(b"receipt")
            save_json_atomic(cfg.state_path("actor_thumb_urls_tmdb.json"),{"A":["https://invalid"]})
            result=fetch(cfg,downloader=lambda _: self.fail("download called"))
            self.assertEqual(result["statuses"]["A"]["status"],"exists")
    def test_pillow_absence_is_passthrough(self):
        real_import=builtins.__import__
        def missing(name,*a,**kw):
            if name=="PIL": raise ImportError
            return real_import(name,*a,**kw)
        with patch("builtins.__import__",side_effect=missing): self.assertEqual(normalize_actor_image(b"raw"),b"raw")
    def test_cli_help_and_status_from_other_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            for args in (["--help"],["movies","--help"],["tv","--help"],["--state-dir",td,"status"]):
                result=subprocess.run([sys.executable,str(ROOT/"harvester.py"),*args],cwd="/",capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
    def test_missing_credentials_are_scoped(self):
        result=subprocess.run([sys.executable,str(ROOT/"harvester.py"),"movies","scan-actors"],env={"PATH":os.environ["PATH"]},capture_output=True,text=True)
        self.assertEqual(result.returncode,2); self.assertIn("TMDB capability unavailable",result.stderr)
    def test_successful_movie_state_survives_refresh_failure(self):
        class Broken:
            def get(self,*args,**kwargs): raise TimeoutError("wobble")
        with tempfile.TemporaryDirectory() as td:
            base=Path(td); movie=base/"Movie"; movie.mkdir()
            nfo=movie/"film.nfo"; nfo.write_text("<movie><title>Film</title><uniqueid type='tmdb'>1</uniqueid><actor><name>A</name></actor></movie>")
            cfg=load_config({"state_dir":base/"state","movie_root":movie,"tv_root":base/"TV"},{},base)
            prior={str(nfo):{"status":"matched","movie":{"title":"Film","tmdb_id":"1","actors":["A"]},"actors":{"A":["https://image/a.jpg"]}}}
            save_json_atomic(cfg.state_path("movie_actor_queue.json"),prior)
            movie_scan(cfg,Broken(),refresh=True)
            self.assertEqual(load_json(cfg.state_path("movie_actor_queue.json"),{})[str(nfo)]["actors"],prior[str(nfo)]["actors"])

if __name__=="__main__": unittest.main()
