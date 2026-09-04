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

Defaults are `/mnt/2tb/Movie`, `/mnt/2tb/TV`, and `state/` beside `harvester.py`. Override them globally with `--movie-root`, `--tv-root`, and `--state-dir`, or with `HARVESTER_MOVIE_ROOT`, `HARVESTER_TV_ROOT`, and `HARVESTER_STATE_DIR`. Relative overrides are resolved against the application directory, not the invoking shell's directory.

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

* `movie_actor_queue.json` — actor-centric contexts and per-actor resolution/failure receipts for resuming.
* `actor_thumb_urls_tmdb.json` — frozen actor image URLs consumed by `fetch-actors`.
* `actor_photo_download_status.json` — actor image download outcomes.
* `tv_show_urls_tvdb.json` — Stage 1 matches, URL-free NFO payloads, separate asset URLs, and Stage 2 receipts.
* `tmdb_api_cache.json` and `tvdb_api_cache.json` — persistent raw GET response caches.

Normal runs resume these files. An actor found in several NFOs is tried through those movie contexts until one resolves. Movie lookup prefers local TMDB ID, then IMDb lookup, then conservative title/year matching. Existing actor files are always receipts; NFO/poster overwrite behavior retains the reference defaults and has explicit opt-outs. Failed transfers are retried only when the corresponding retry control permits it. A transient refresh failure does not replace a prior successful match. Ambiguous title-only TV searches remain staged as ambiguous for human correction rather than selecting recklessly. CLI results are compact summaries; detailed receipts remain in these JSON files. Malformed state is never silently replaced: jobs report an actionable read error, while `status` marks the affected file unreadable and continues checking the others.

## Validation

No live provider or real library is used by the suite:

```bash
python3 -m unittest discover -s tests -v
```

Syntax check: `python3 -m compileall -q harvester.py harvester_core tests`.

## Changelog

Entries are listed newest first. Each entry describes behavior that changed rather than
repeating commit messages.

### 2026-09-04 — Initial CLI consolidation

#### Added

* Added one `harvester.py` command-line entry point for checking local state, finding
  movie actor images through TMDB, and scanning or materializing TV metadata through
  TVDB.
* Added resumable JSON work files and provider caches. State updates use atomic file
  replacement so an interrupted write does not leave a partly written manifest.
* Added configuration through command-line options, environment variables, and an
  ignored `keys_and_tokens.txt` file. Local status and help commands do not require
  provider credentials.
* Added callable jobs with reporter callbacks, keeping progress reporting outside the
  harvesting work so the same jobs can be used without the CLI.
* Added regression tests for matching, retries, cached provider responses, malformed
  state, actor image handling, and TV NFO and poster output.

#### Changed

* Moved the behavior from the standalone reference scripts into provider, job,
  configuration, image, and storage modules while keeping Python's standard library as
  the only required runtime dependency.
* Kept movie and TV runs restartable: existing files count as completed work, prior
  successful matches survive transient refresh failures, and ambiguous TV title matches
  wait for human correction.
* Split TV harvesting into a networked scan stage and an offline materialization stage.
  Materialization reads frozen metadata and image URLs and never creates a TVDB client.

#### Fixed

* Made TV materialization safe to run repeatedly without duplicating actors, artwork,
  or XML elements, while retaining explicit controls for replacing NFO and poster files.
* Restored the TV NFO renderer import so materialization can write `tvshow.nfo` files.
