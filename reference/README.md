# Harvester reference scripts

These are the proven one-off scripts that motivated the Harvester consolidation.
They are kept here temporarily as behavioral reference while the permanent CLI,
provider adapters, jobs, configuration, and reporting layers are built outside
`reference/`.

Do not treat this directory as the target architecture. Preserve the useful
behavioral contracts: durable JSON work queues/manifests, resumability, cautious
matching, API caching/backoff, atomic writes, separate resolve/materialize phases,
and optional image normalization.

## Contents

- `movie_actor_tmdb_scanner.py` — scans movie NFOs, resolves actors through TMDB
  using known movie context, and produces the actor-image URL work data.
- `movie_actor_photo_downloader.py` — consumes resolved actor-image URLs and
  materializes the local movie `.actors` cache.
- `tv_tvdb_scan.py` — Stage 1 TVDB resolver; builds a durable manifest without
  writing into the TV library.
- `tv_tvdb_materialize.py` — Stage 2 materializer; consumes the frozen manifest
  and writes TV NFOs, posters, and actor images without calling the TVDB API.

Credentials have deliberately been removed from these reference copies. The
permanent Harvester should obtain provider credentials from optional local
configuration/environment (for example `keys_and_tokens.txt`) rather than source.

Once the consolidated implementation has regression coverage for the preserved
behavior and no longer depends on these files, this directory can be deleted.
