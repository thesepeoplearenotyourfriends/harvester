# Harvester

Harvester is a small, CLI-first home for resumable movie-actor and TV metadata/image harvesting. It is not a UI, media organizer, player, or server. Its required runtime is Python's standard library.

## Commands

```text
python3 harvester.py status
python3 harvester.py movies scan-actors [--limit N] [--rebuild] [--refresh] [--fallback]
python3 harvester.py movies fetch-actors [--limit N] [--retry-failed] [--overwrite]
python3 harvester.py movies scan [--limit N] [--rebuild] [--refresh]
python3 harvester.py movies materialize [--limit N] [--overwrite-nfo] [--overwrite-poster]
python3 harvester.py tv scan [--limit N] [--rebuild] [--refresh] [--retry-ambiguous]
python3 harvester.py tv materialize [--limit N] [--no-overwrite-nfo] [--no-overwrite-poster]
python3 harvester.py api providers|get|list|search|inventory|rescan|refresh ...
```

Run `python3 harvester.py --help` and nested `--help` for the complete map. Movie and TV `scan` commands resolve identities and freeze metadata/asset URLs. Their `materialize` commands consume those files without constructing or calling a provider client. `status` and API get/list/inventory are local-only and need neither credentials nor network. Every API stdout line is a JSON object; long operations end with exactly one result or error record.

## Configuration

Defaults are `/mnt/2tb/Movie`, `/mnt/2tb/TV`, and `state/` beside `harvester.py`. Override them globally with `--movie-root`, `--tv-root`, and `--state-dir`, or with `HARVESTER_MOVIE_ROOT`, `HARVESTER_TV_ROOT`, and `HARVESTER_STATE_DIR`. Relative overrides are resolved against the application directory, not the invoking shell's directory.

An optional ignored `keys_and_tokens.txt` beside `harvester.py` accepts:

```text
TMDB_API_KEY=...
TMDB_BEARER_TOKEN=...
TVDB_API_KEY=...
TVDB_PIN=...
HARVESTER_SOCKS5=127.0.0.1:1080
HARVESTER_SOCKS5_USERNAME=...
HARVESTER_SOCKS5_PASSWORD=...
```

Blank lines, comments beginning with `#`, and unknown keys are ignored. Precedence is explicit command/runtime override, then same-named environment variable, then the file, then defaults. Secrets and provider bearer tokens are not stored in caches or normal output.

Pillow is optional and imported only while treating actor images. Without it, downloaded bytes are preserved unchanged.

Network requests use direct sockets by default. Set `HARVESTER_SOCKS5=127.0.0.1:1080`
or pass `--socks5 127.0.0.1:1080` to route TMDB, TVDB, and image downloads through
SOCKS5. Hostnames are resolved by the proxy, as with `curl --socks5-hostname`; Harvester
does not replace sockets globally. Authenticated proxies accept
`--socks5 user:password@host:port`, or the `HARVESTER_SOCKS5_USERNAME` and
`HARVESTER_SOCKS5_PASSWORD` environment variables. Do not put a password directly on
the command line on a multi-user system because process listings may expose it.

## Durable state and restart behavior

All state is ordinary atomically replaced JSON:

* `movie_actor_queue.json` — actor-centric contexts and per-actor resolution/failure receipts for resuming.
* `actor_thumb_urls_tmdb.json` — frozen actor image URLs consumed by `fetch-actors`.
* `actor_photo_download_status.json` — actor image download outcomes.
* `tv_show_urls_tvdb.json` — Stage 1 matches, URL-free NFO payloads, separate asset URLs, and Stage 2 receipts.
* `movie_manifest_tmdb.json` — frozen movie identity, NFO payloads, poster URLs, errors, and materialization receipts.
* `tmdb_api_cache.json` and `tvdb_api_cache.json` — persistent raw GET response caches.

Normal runs resume these files. An actor found in several NFOs is tried through those movie contexts until one resolves. Movie lookup prefers local TMDB ID, then IMDb lookup, then conservative title/year matching. Existing actor files are always receipts; NFO/poster overwrite behavior retains the reference defaults and has explicit opt-outs. Failed transfers are retried only when the corresponding retry control permits it. A transient refresh failure does not replace a prior successful match. Ambiguous title-only TV searches remain staged as ambiguous for human correction rather than selecting recklessly. CLI results are compact summaries; detailed receipts remain in these JSON files. Malformed state is never silently replaced: jobs report an actionable read error, while `status` marks the affected file unreadable and continues checking the others.

For offline integrations, `api get movie|show IDENTIFIER` remains the raw durable-record
primitive. `api inspect movie|show IDENTIFIER` instead reports the artifacts currently
on disk: the owning directory, parsed local NFO fields, poster and video presence, and
the manifest identities behind that view. `api list movies|shows --artifacts` provides
compact presentation rows for queues. Records remain identity-based unless the caller
also supplies `--group-directories`; the UI uses that grouping only for Missing Poster.
Grouped movie entries retain every underlying identity and report ambiguous ownership
rather than pretending each NFO owns the same poster context. These inspection
operations make no provider requests and do not change durable state.

## Validation

No live provider or real library is used by the suite:

```bash
python3 -m unittest discover -s tests -v
```

Syntax check: `python3 -m compileall -q harvester.py harvester-ui.py harvester_ui.py harvester_core tests`.

## Optional desktop UI

`python3 harvester-ui.py` opens the compact Severin workbench when the optional
`severin` package and a graphical session are available. Overview and its work
queues are backed by `harvester.py api`; the UI host does not read Harvester
manifests itself.

Queue clients can request compact records with `api list actors|movies|shows
--brief` and apply `--status` or `--missing` filters. `api search QUERY` searches
actor, movie, and show identities in durable state without contacting a provider.
`api rescan` rebuilds all local censuses while retaining provider results for
identities that still exist. It refuses unavailable or suspiciously empty library
roots before writing state, retains manifest run history, and prunes frozen actor
URL work to the actors still in local NFOs. It
does not construct a provider, download an image, or materialize metadata.
The desktop UI opens on an inventory overview. Its Work menu and resizable context
pane open the same queues; Missing Actor Images decodes dropped or selected images
in the browser, fits them within 185 × 278 pixels, and sends a small canonical JPEG
for the `.actors` file. Refresh from API invokes the existing targeted actor image
refresh.

Bulk work has one application-level state shown either in the persistent bottom strip,
its slide-up drawer, or the full **Work → Bulk** workspace. Problem queues expose
**Scan All**, which freezes that queue's current record identities before starting a
semantic, allowlisted operation. Leaving the queue or closing the drawer does not stop
the work. Broad materialization preserves files that already exist rather than treating
the workflow as an overwrite request; Bulk state is session-only and is not a job-history
store.

List and search results are written atomically to filter-specific, versioned files
under `.cache/ui/`. Only an asset descriptor crosses the Severin bridge, and the
renderer reads the collection through `asset://com.harvester.app/`. The cache is
not authoritative and `rm -rf .cache/` is always safe; the next request recreates
any missing collection file.

## Changelog

Entries are listed newest first. Each entry describes behavior that changed rather than
repeating commit messages.

### 2026-09-04 — Initial Severin desktop UI

#### Added

* Added an optional 800 × 400 Severin launcher and a plain HTML/CSS desktop
  browser for inventory, provider, movie, TV, and actor records.
* Added a capability-only deferred bridge that invokes the existing
  `harvester.py api` NDJSON interface with fixed argument shapes.
* Added versioned, disposable `.cache/ui/collection-*.json` files for list and
  search results that would exceed Severin's bridge frame. Python owns atomic
  writes and the renderer reads collections through the package's `asset://`
  namespace.

### 2026-09-04 — Reference parity audit

#### Fixed

* Restored the reference checkpoint cadences, request throttles, timeouts, attempt
  counts, TVDB User-Agent, and actor-image request headers. TMDB requests use the
  current `harvester/1` identifier rather than the historical reference string.
* TV scan and materialization now save their manifests when interrupted.
* Actor download receipts again include timestamps, source URLs, and content types.
* TV materialization no longer reports a downloaded poster as failed only because a
  stale alternate-format poster could not be removed.

The mechanical comparison and the deliberately retained integration differences are
recorded in [`reference/PARITY_AUDIT.md`](reference/PARITY_AUDIT.md).

### 2026-09-04 — Optional SOCKS5 transport

#### Added

* Added stdlib-only SOCKS5 transport selection for TMDB, TVDB, actor images, and TV
  artwork. SOCKS5 requests send destination hostnames to the proxy for remote DNS.
* Added optional SOCKS5 username/password authentication. Direct sockets remain the
  default.

#### Fixed

* TMDB HTTP failures now retain the API's sanitized `status_code` and `status_message`
  while omitting API keys from raised errors.

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
