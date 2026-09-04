# Harvester

Harvester is a small, CLI-first home for resumable movie-actor and TV metadata/image harvesting. It is not a UI, media organizer, player, or server. Its required runtime is Python's standard library.

## Commands

```text
python3 harvester.py status
python3 harvester.py movies scan-actors [--limit N] [--rebuild] [--refresh] [--fallback]
python3 harvester.py movies fetch-actors [--limit N] [--retry-failed] [--overwrite]
python3 harvester.py tv scan [--limit N] [--rebuild] [--refresh] [--retry-ambiguous]
python3 harvester.py tv materialize [--limit N] [--no-overwrite-nfo] [--no-overwrite-poster]
```

Run `python3 harvester.py --help` and nested `--help` for the complete map. Stage 1 `tv scan` resolves titles and freezes metadata/asset URLs. Stage 2 `tv materialize` consumes that file and **never creates or calls a TVDB client**. `status` is local-only and needs neither credentials nor network.

## Configuration

Defaults are `/mnt/2tb/Movie`, `/mnt/2tb/TV`, and `state/` beside `harvester.py`. Override them globally with `--movie-root`, `--tv-root`, and `--state-dir`, or with `HARVESTER_MOVIE_ROOT`, `HARVESTER_TV_ROOT`, and `HARVESTER_STATE_DIR`. Resolution is independent of the current working directory.

An optional ignored `keys_and_tokens.txt` beside `harvester.py` accepts:

```text
TMDB_API_KEY=...
TMDB_BEARER_TOKEN=...
TVDB_API_KEY=...
TVDB_PIN=...
```

Blank lines, comments beginning with `#`, and unknown keys are ignored. Precedence is explicit command/runtime override, then same-named environment variable, then the file, then defaults. Secrets and provider bearer tokens are not stored in caches or normal output.

Pillow is optional and imported only while treating actor images. Without it, downloaded bytes are preserved unchanged.

## Durable state and restart behavior

All state is ordinary atomically replaced JSON:

* `movie_actor_queue.json` — per-NFO scan/resolution receipts for resuming.
* `actor_thumb_urls_tmdb.json` — frozen actor image URLs consumed by `fetch-actors`.
* `actor_photo_download_status.json` — actor image download outcomes.
* `tv_show_urls_tvdb.json` — Stage 1 matches, URL-free NFO payloads, separate asset URLs, and Stage 2 receipts.
* `tmdb_api_cache.json` and `tvdb_api_cache.json` — persistent raw GET response caches.

Normal runs resume these files. Existing actor files are always receipts; NFO/poster overwrite behavior retains the reference defaults and has explicit opt-outs. Failed transfers are retried only when the corresponding retry control permits it. A transient refresh failure does not replace a prior successful match. Ambiguous title-only TV searches remain staged as ambiguous for human correction rather than selecting recklessly.

## Validation

No live provider or real library is used by the suite:

```bash
python3 -m unittest discover -s tests -v
```

Syntax check: `python3 -m compileall -q harvester.py harvester_core tests`.
