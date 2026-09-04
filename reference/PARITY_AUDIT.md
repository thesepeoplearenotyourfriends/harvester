# Reference behavior parity audit

This audit compares the four scripts in this directory with their callable Harvester
jobs and provider adapters. The reference scripts are the behavioral specification;
the audit did not rename or consolidate their controls.

## Restored controls

| Reference behavior | Harvester location | Restored behavior |
| --- | --- | --- |
| TMDB scanner `save_every=50` | `jobs/movie_actor_scan.py` | Queue and URL output checkpoint every 50 changed actors, plus a final save. Unknown receipt states are skipped. |
| Actor downloader `TIMEOUT=30`, `SLEEP_BETWEEN=0.0`, `SAVE_EVERY=25` | `jobs/movie_actor_fetch.py` | The callable job has the same defaults and records timestamps, source content type, and source URL. |
| Actor downloader User-Agent | `jobs/movie_actor_fetch.py` | Requests use `local-tmdb-actor-photo-gulper/1.0`. |
| TMDB language, timeout, attempts, and User-Agent | `providers/tmdb.py` | Defaults are `en-US`, 10 seconds, five attempts, and `local-nfo-tmdb-thumb-cache/1.0`. |
| TVDB timeout, attempts, and User-Agent | `providers/tvdb.py` | Defaults are 20 seconds, five attempts, and `local-tv-tvdb-url-scanner/1.0`. |
| TV scan `SLEEP_BETWEEN_SHOWS=1` and `SAVE_EVERY_SHOWS=1` | `jobs/tv_scan.py` | The job exposes the same defaults, checkpoints at that cadence, and saves on Ctrl-C. As in the reference loop, unresolved searches skip the inter-show sleep. |
| TV materializer timeout, attempts, throttle, and `SAVE_EVERY_CHANGES=25` | `jobs/tv_materialize.py` | Defaults remain 30 seconds, four attempts, 0.5 seconds after a downloaded actor image, and 25 changed artifacts per checkpoint. |
| TV materializer interruption and run receipts | `jobs/tv_materialize.py` | Ctrl-C writes the manifest and `last_materialize_run`; normal completion does the same. |
| Poster format replacement | `jobs/tv_materialize.py` | Failure to remove a stale alternate poster format does not relabel the successfully written poster as a failed download. |
| TV root validation | `jobs/tv_materialize.py` | Materialization rejects a missing TV library before trying to create `.actors`. |

## Deliberate non-behavioral differences

These differences are required by the repository's governing design and do not change
provider matching, retry, overwrite, cache, image, or checkpoint decisions:

* Paths and credentials come from `Config` instead of module constants.
* Jobs report events through a callback and return compact summaries instead of printing.
* Provider transport is injectable so the existing optional SOCKS5 layer can be used.
* State writes also `fsync` the temporary file before the same-directory atomic replace.
* Provider exceptions omit credentials and bearer tokens from messages.
* Stage 2 accepts frozen manifest data and never constructs a TVDB client.

No reference limit, batch size, overwrite/skip rule, cache key rule, image size/quality,
retry class, backoff default, timeout, or provider matching oddity was otherwise found
missing from the corresponding Harvester path.
