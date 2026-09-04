#!/usr/bin/env python3
"""The single Harvester command-line frontend."""
import argparse
import contextlib
import io
import json
import re
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
    scan_movies = movies.add_parser("scan", help="Stage 1: freeze TMDB movie metadata and poster URLs")
    add_limit(scan_movies)
    scan_movies.add_argument("--rebuild", action="store_true")
    scan_movies.add_argument("--refresh", action="store_true")
    materialize_movies = movies.add_parser("materialize", help="Stage 2: write from the frozen movie manifest")
    add_limit(materialize_movies)
    materialize_movies.add_argument("--overwrite-nfo", action="store_true")
    materialize_movies.add_argument("--overwrite-poster", action="store_true")

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

    api = commands.add_parser(
        "api", help="machine-oriented queries and targeted operations"
    ).add_subparsers(dest="api_command", required=True)
    api.add_parser("providers", help="list registered provider capabilities")
    get = api.add_parser("get", help="get one durable record").add_subparsers(dest="kind", required=True)
    for kind in ("actor", "movie", "show"):
        item = get.add_parser(kind)
        item.add_argument("identifier")
    listing = api.add_parser("list", help="list durable records").add_subparsers(dest="kind", required=True)
    for plural, kind in (("actors", "actor"), ("movies", "movie"), ("shows", "show")):
        item = listing.add_parser(plural)
        item.set_defaults(kind=kind)
        item.add_argument("--status")
        item.add_argument("--limit", type=int)
        if kind in ("movie", "show"):
            item.add_argument("--missing", choices=("nfo", "poster"))
    api.add_parser("inventory", help="summarize durable state")
    refresh = api.add_parser("refresh", help="refresh explicitly named records").add_subparsers(dest="kind", required=True)
    aspects = {"actor": ("identity", "image", "all"), "movie": ("identity", "metadata", "nfo", "poster", "actors", "all"), "show": ("identity", "metadata", "nfo", "poster", "actors", "all")}
    for kind, choices in aspects.items():
        item = refresh.add_parser(kind)
        item.add_argument("identifiers", nargs="+")
        item.add_argument("--aspect", choices=choices, default="all")
    return root


def report(event: Event):
    details = " ".join(f"{key}={value}" for key, value in event.data.items())
    print(f"[{event.kind}] {event.message}: {details}")


def main(argv=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "api" in raw_argv:
        # argparse's ordinary usage prose would violate the API stdout/stderr
        # contract. API help remains normal help, while parse failures are JSON.
        parse_errors = io.StringIO()
        try:
            with contextlib.redirect_stderr(parse_errors):
                args = parser().parse_args(raw_argv)
        except SystemExit as error:
            if error.code == 0:
                raise
            detail = parse_errors.getvalue().strip().splitlines()
            message = detail[-1] if detail else "invalid API arguments"
            _ndjson({"schema": 1, "type": "error", "ok": False, "error": message})
            return int(error.code or 2)
    else:
        args = parser().parse_args(raw_argv)
    values = vars(args)
    fields = (
        "state_dir", "movie_root", "tv_root", "tmdb_api_key",
        "tmdb_bearer_token", "tvdb_api_key", "tvdb_pin", "socks5",
        "socks5_username", "socks5_password",
    )
    config = load_config({field: values.get(field) for field in fields})
    try:
        if args.command == "api":
            return api_main(args, config)
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

        if args.command == "movies" and args.movie_command == "scan":
            from harvester_core.jobs.movie_scan import run
            from harvester_core.providers.tmdb import TMDBClient
            provider = TMDBClient(config.tmdb_api_key, config.tmdb_bearer_token, config.state_path("tmdb_api_cache.json"), transport)
            result = run(config, provider, report, args.limit, args.rebuild, args.refresh)
        elif args.command == "movies" and args.movie_command == "materialize":
            from harvester_core.jobs.movie_materialize import run
            result = run(config, report, args.limit, args.overwrite_nfo, args.overwrite_poster, transport=transport)
        elif args.command == "movies" and args.movie_command == "scan-actors":
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


def _safe_machine_value(value, secrets=()):
    """Remove credentials even when an exception embedded them in a URL."""
    if isinstance(value, dict):
        return {key: _safe_machine_value(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_machine_value(item, secrets) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(https?://)[^/@\s]+@", r"\1[redacted]@", value)
        for secret in secrets:
            if secret:
                value = value.replace(str(secret), "[redacted]")
    return value


def _ndjson(record, secrets=()):
    print(json.dumps(_safe_machine_value(record, secrets), ensure_ascii=False,
                     separators=(",", ":"), default=str))


def api_main(args, config):
    """Run the strict NDJSON frontend; every path emits one terminal record."""
    secrets = (config.tmdb_api_key, config.tmdb_bearer_token, config.tvdb_api_key,
               config.tvdb_pin, config.socks5_username, config.socks5_password)
    def terminal(result):
        _ndjson({"schema": 1, "type": "result", "ok": True, "result": result}, secrets)

    def api_report(event):
        data = dict(event.data)
        _ndjson({"schema": 1, "type": "event", "event": event.kind,
                 "kind": data.pop("target_kind", None), "id": data.pop("id", event.message),
                 **data}, secrets)
    try:
        if args.api_command == "providers":
            from harvester_core.providers.profiles import profiles
            terminal({"providers": profiles(config)})
        elif args.api_command in ("get", "list", "inventory"):
            from harvester_core.api import get_record, inventory, list_records
            if args.api_command == "get":
                terminal(get_record(config, args.kind, args.identifier))
            elif args.api_command == "list":
                terminal({"items": list_records(config, args.kind, args.status, args.limit, getattr(args, "missing", None))})
            else:
                terminal(inventory(config))
        else:
            from harvester_core.transport import transport_from_config
            transport = transport_from_config(config)
            if args.kind == "actor":
                from harvester_core.jobs.movie_actor_scan import run
                from harvester_core.providers.tmdb import TMDBClient
                provider = TMDBClient(config.tmdb_api_key, config.tmdb_bearer_token, config.state_path("tmdb_api_cache.json"), transport)
                result = run(config, provider, api_report, refresh=True, retry_failed=True, targets=args.identifiers)
                if args.aspect in ("image", "all"):
                    from harvester_core.jobs.movie_actor_fetch import run as fetch
                    result["materialize"] = fetch(config, api_report, retry_failed=True, overwrite=True, transport=transport, targets=args.identifiers)
            elif args.kind == "movie":
                from harvester_core.api import get_record
                targets = [get_record(config, "movie", value)["local_target"] for value in args.identifiers]
                if args.aspect in ("identity", "metadata", "actors", "all"):
                    from harvester_core.jobs.movie_scan import run
                    from harvester_core.providers.tmdb import TMDBClient
                    provider = TMDBClient(config.tmdb_api_key, config.tmdb_bearer_token, config.state_path("tmdb_api_cache.json"), transport)
                    result = run(config, provider, api_report, refresh=True, targets=targets)
                else:
                    result = {"processed": 0}
                if args.aspect in ("nfo", "poster", "all"):
                    from harvester_core.jobs.movie_materialize import run as materialize
                    result["materialize"] = materialize(
                        config, api_report,
                        overwrite_nfo=args.aspect in ("nfo", "all"),
                        overwrite_poster=args.aspect in ("poster", "all"),
                        targets=targets, transport=transport,
                        write_nfo=args.aspect in ("nfo", "all"),
                        write_poster=args.aspect in ("poster", "all"),
                    )
            else:
                from harvester_core.api import get_record
                targets = [get_record(config, "show", value)["local_target"] for value in args.identifiers]
                if args.aspect in ("identity", "metadata", "actors", "all"):
                    from harvester_core.jobs.tv_scan import run
                    from harvester_core.providers.tvdb import TVDBClient
                    provider = TVDBClient(config.tvdb_api_key, config.tvdb_pin, config.state_path("tvdb_api_cache.json"), transport)
                    result = run(config, provider, api_report, refresh=True, retry_errors=True, retry_ambiguous=True, retry_not_found=True, targets=targets)
                else:
                    result = {"processed": 0}
                if args.aspect in ("nfo", "poster", "actors", "all"):
                    from harvester_core.jobs.tv_materialize import run as materialize
                    result["materialize"] = materialize(config, api_report,
                        overwrite_nfo=args.aspect in ("nfo", "all"),
                        overwrite_poster=args.aspect in ("poster", "all"),
                        targets=targets, transport=transport)
            terminal(result)
        return 0
    except (ValueError, RuntimeError, FileNotFoundError, KeyError, KeyboardInterrupt) as error:
        _ndjson({"schema": 1, "type": "error", "ok": False, "error": str(error)}, secrets)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
