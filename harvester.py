#!/usr/bin/env python3
"""The single Harvester command-line frontend."""
import argparse
import json
import sys

from harvester_core.config import load_config
from harvester_core.events import Event
from harvester_core.storage import load_json, StateReadError


def add_limit(parser):
    parser.add_argument("--limit", type=int)


def parser():
    root = argparse.ArgumentParser(
        description="Resumable movie/TV metadata and image harvester"
    )
    root.add_argument("--state-dir")
    root.add_argument("--movie-root")
    root.add_argument("--tv-root")
    root.add_argument("--tmdb-api-key", help=argparse.SUPPRESS)
    root.add_argument("--tmdb-bearer-token", help=argparse.SUPPRESS)
    root.add_argument("--tvdb-api-key", help=argparse.SUPPRESS)
    root.add_argument("--tvdb-pin", help=argparse.SUPPRESS)
    root.add_argument(
        "--socks5", metavar="[USER:PASS@]HOST:PORT",
        help="route provider and image requests through SOCKS5 (remote DNS)",
    )
    root.add_argument("--socks5-username", help=argparse.SUPPRESS)
    root.add_argument("--socks5-password", help=argparse.SUPPRESS)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="inspect local durable state (offline)")

    movies = commands.add_parser(
        "movies", help="movie actor workflows"
    ).add_subparsers(dest="movie_command", required=True)
    scan = movies.add_parser(
        "scan-actors", help="scan NFOs and resolve actors through TMDB"
    )
    add_limit(scan)
    scan.add_argument("--rebuild", action="store_true")
    scan.add_argument("--refresh", action="store_true")
    scan.add_argument("--retry-failed", action="store_true")
    scan.add_argument("--fallback", action="store_true")
    fetch = movies.add_parser(
        "fetch-actors", help="download actor images from frozen URL work"
    )
    add_limit(fetch)
    fetch.add_argument("--retry-failed", action="store_true")
    fetch.add_argument("--overwrite", action="store_true")

    tv = commands.add_parser(
        "tv", help="two-stage TV workflows"
    ).add_subparsers(dest="tv_command", required=True)
    scan = tv.add_parser("scan", help="Stage 1: resolve folders into a manifest")
    add_limit(scan)
    scan.add_argument("--rebuild", action="store_true")
    scan.add_argument("--refresh", action="store_true")
    scan.add_argument("--retry-ambiguous", action="store_true")
    scan.add_argument("--retry-not-found", action="store_true")
    scan.add_argument("--no-retry-errors", action="store_true")
    materialize = tv.add_parser(
        "materialize", help="Stage 2: write only from the frozen manifest"
    )
    add_limit(materialize)
    materialize.add_argument("--no-overwrite-nfo", action="store_true")
    materialize.add_argument("--no-overwrite-poster", action="store_true")
    materialize.add_argument("--no-retry-failed", action="store_true")
    return root


def report(event: Event):
    details = " ".join(f"{key}={value}" for key, value in event.data.items())
    print(f"[{event.kind}] {event.message}: {details}")


def main(argv=None):
    args = parser().parse_args(argv)
    values = vars(args)
    fields = (
        "state_dir", "movie_root", "tv_root", "tmdb_api_key",
        "tmdb_bearer_token", "tvdb_api_key", "tvdb_pin", "socks5",
        "socks5_username", "socks5_password",
    )
    config = load_config({field: values.get(field) for field in fields})
    try:
        if args.command == "status":
            print("State directory:", config.state_dir)
            names = (
                "movie_actor_queue.json", "actor_thumb_urls_tmdb.json",
                "actor_photo_download_status.json", "tv_show_urls_tvdb.json",
                "tmdb_api_cache.json", "tvdb_api_cache.json",
            )
            for name in names:
                try:
                    data = load_json(config.state_path(name), None)
                    state = "absent" if data is None else "present"
                except StateReadError as error:
                    state = f"unreadable ({error})"
                print(f"{name}: {state}")
            return 0

        from harvester_core.transport import transport_from_config
        transport = transport_from_config(config)

        if args.command == "movies" and args.movie_command == "scan-actors":
            from harvester_core.jobs.movie_actor_scan import run
            from harvester_core.providers.tmdb import TMDBClient
            provider = TMDBClient(
                config.tmdb_api_key, config.tmdb_bearer_token,
                config.state_path("tmdb_api_cache.json"),
                transport,
            )
            result = run(
                config, provider, report, args.limit, args.rebuild,
                args.refresh, args.fallback, args.retry_failed,
            )
        elif args.command == "movies":
            from harvester_core.jobs.movie_actor_fetch import run
            result = run(
                config, report, args.limit, args.retry_failed, args.overwrite,
                transport=transport,
            )
        elif args.tv_command == "scan":
            from harvester_core.jobs.tv_scan import run
            from harvester_core.providers.tvdb import TVDBClient
            provider = TVDBClient(
                config.tvdb_api_key, config.tvdb_pin,
                config.state_path("tvdb_api_cache.json"),
                transport,
            )
            result = run(
                config, provider, report, args.limit, args.rebuild, args.refresh,
                not args.no_retry_errors, args.retry_ambiguous,
                args.retry_not_found,
            )
        else:
            from harvester_core.jobs.tv_materialize import run
            result = run(
                config, report, args.limit, not args.no_overwrite_nfo,
                not args.no_overwrite_poster, not args.no_retry_failed,
                transport=transport,
            )
        print(json.dumps(result, indent=2, default=str))
        return 0
    except (ValueError, RuntimeError, FileNotFoundError) as error:
        print(f"harvester: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
