#!/usr/bin/env python3
"""The single Harvester command-line frontend."""
import argparse, json, sys
from harvester_core.config import load_config
from harvester_core.events import Event
from harvester_core.storage import load_json

def parser():
    root=argparse.ArgumentParser(description="Resumable movie/TV metadata and image harvester")
    root.add_argument("--state-dir"); root.add_argument("--movie-root"); root.add_argument("--tv-root")
    root.add_argument("--tmdb-api-key",help=argparse.SUPPRESS); root.add_argument("--tmdb-bearer-token",help=argparse.SUPPRESS); root.add_argument("--tvdb-api-key",help=argparse.SUPPRESS); root.add_argument("--tvdb-pin",help=argparse.SUPPRESS)
    commands=root.add_subparsers(dest="command",required=True)
    commands.add_parser("status",help="inspect local durable state (offline)")
    movies=commands.add_parser("movies",help="movie actor workflows").add_subparsers(dest="movie_command",required=True)
    scan=movies.add_parser("scan-actors",help="scan NFOs and resolve actors through TMDB"); common(scan); scan.add_argument("--rebuild",action="store_true"); scan.add_argument("--refresh",action="store_true"); scan.add_argument("--fallback",action="store_true",help="allow conservative person-search fallback")
    fetch=movies.add_parser("fetch-actors",help="download actor images from frozen URL work"); common(fetch); fetch.add_argument("--retry-failed",action="store_true"); fetch.add_argument("--overwrite",action="store_true")
    tv=commands.add_parser("tv",help="two-stage TV workflows").add_subparsers(dest="tv_command",required=True)
    scan=tv.add_parser("scan",help="Stage 1: resolve TV folders into a manifest"); common(scan); scan.add_argument("--rebuild",action="store_true"); scan.add_argument("--refresh",action="store_true"); scan.add_argument("--retry-ambiguous",action="store_true"); scan.add_argument("--retry-not-found",action="store_true"); scan.add_argument("--no-retry-errors",action="store_true")
    materialize=tv.add_parser("materialize",help="Stage 2: write only from the frozen manifest"); common(materialize); materialize.add_argument("--no-overwrite-nfo",action="store_true"); materialize.add_argument("--no-overwrite-poster",action="store_true"); materialize.add_argument("--no-retry-failed",action="store_true")
    return root
def common(value): value.add_argument("--limit",type=int)
def report(event: Event): print(f"[{event.kind}] {event.message}: " + " ".join(f"{k}={v}" for k,v in event.data.items()))
def main(argv=None):
    args=parser().parse_args(argv); values=vars(args); config=load_config({k:values.get(k) for k in ("state_dir","movie_root","tv_root","tmdb_api_key","tmdb_bearer_token","tvdb_api_key","tvdb_pin")})
    try:
        if args.command=="status":
            print("State directory:",config.state_dir)
            for name in ("movie_actor_queue.json","actor_thumb_urls_tmdb.json","actor_photo_download_status.json","tv_show_urls_tvdb.json","tmdb_api_cache.json","tvdb_api_cache.json"):
                data=load_json(config.state_path(name),None); print(f"{name}: absent" if data is None else f"{name}: present ({len(data) if hasattr(data,'__len__') else 'unknown'} entries)")
            return 0
        if args.command=="movies" and args.movie_command=="scan-actors":
            from harvester_core.providers.tmdb import TMDBClient
            from harvester_core.jobs.movie_actor_scan import run
            provider=TMDBClient(config.tmdb_api_key,config.tmdb_bearer_token,config.state_path("tmdb_api_cache.json")); result=run(config,provider,report,args.limit,args.rebuild,args.refresh,args.fallback)
        elif args.command=="movies":
            from harvester_core.jobs.movie_actor_fetch import run
            result=run(config,report,args.limit,args.retry_failed,args.overwrite)
        elif args.tv_command=="scan":
            from harvester_core.providers.tvdb import TVDBClient
            from harvester_core.jobs.tv_scan import run
            provider=TVDBClient(config.tvdb_api_key,config.tvdb_pin,config.state_path("tvdb_api_cache.json")); result=run(config,provider,report,args.limit,args.rebuild,args.refresh,not args.no_retry_errors,args.retry_ambiguous,args.retry_not_found)
        else:
            from harvester_core.jobs.tv_materialize import run
            result=run(config,report,args.limit,not args.no_overwrite_nfo,not args.no_overwrite_poster,not args.no_retry_failed)
        print(json.dumps(result,indent=2,default=str)); return 0
    except (ValueError,RuntimeError,FileNotFoundError) as error: print(f"harvester: {error}",file=sys.stderr); return 2
if __name__=="__main__": raise SystemExit(main())
