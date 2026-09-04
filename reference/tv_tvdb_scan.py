#!/usr/bin/env python3
"""
Stage 1: scan a TV-library root, resolve each immediate title folder through
TheTVDB v4, and build a durable work file for a later materializer/downloader.

This script intentionally DOES NOT write into the TV library.  It only writes
beside itself:

  tv_show_urls_tvdb.json   resolved URL-free NFO metadata plus acquisition URLs
  tvdb_api_cache.json      raw GET-response cache, so resumes stay cheap

Stage 2 consumes tv_show_urls_tvdb.json and may write:

  TV Shows/<Show>/show.nfo
  TV Shows/<Show>/poster.jpg or poster.png
  TV Shows/.actors/<Actor_Name>.jpg

The work file keeps NFO content and remote asset URLs separate.  NFO metadata
contains no web URLs, local paths, poster filenames, or actor-file references.
"""

import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

# The one library-location setting Stage 1 needs.
TV_SHOWS_DIR = "/mnt/2tb/TV"

# Prefer environment variables:
#
#   export TVDB_API_KEY='...'
#   export TVDB_PIN='...'          # only if your key/account needs a PIN
#
# Pasting values below also works, but leaving secrets out of source is nicer.
TVDB_API_KEY = ""
TVDB_PIN = ""

# Files live beside this script, not inside the TV library.
WORK_FILE = "tv_show_urls_tvdb.json"
TVDB_CACHE_FILE = "tvdb_api_cache.json"

# TheTVDB returns artwork URLs directly.  No separate image configuration call
# is necessary.
TVDB_API_BASE = "https://api4.thetvdb.com/v4"
TVDB_ARTWORK_BASE = "https://artworks.thetvdb.com"

# A zero sleep is fine for ordinary use.  The client backs off for throttling,
# transient server failures, and a network wobble.
SLEEP_BETWEEN_SHOWS = 1
SAVE_EVERY_SHOWS = 1

# Normal restart behavior:
#   - matched records are left alone
#   - transient errors are retried
#   - ambiguous/not-found names wait for a rename or an explicit retry switch
REBUILD_WORK_FILE = False
REFRESH_MATCHED = False
RETRY_ERRORS = True
RETRY_AMBIGUOUS = False
RETRY_NOT_FOUND = False

# Set to a small number for a first live nibble, then put it back to None.
LIMIT = None
VERBOSE = True

USER_AGENT = "local-tv-tvdb-url-scanner/1.0"
REQUEST_TIMEOUT = 20
REQUEST_ATTEMPTS = 5


# ---------------------------------------------------------------------
# Small durable-storage helpers
# ---------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def script_dir():
    try:
        return Path(__file__).resolve().parent
    except NameError:
        return Path.cwd()


def json_load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: could not read {path}: {e!r}", file=sys.stderr)
        return default


def json_save_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    os.replace(tmp, path)


def unique_strings(values):
    out = []
    seen = set()

    for value in values:
        if value is None:
            continue

        value = str(value).strip()
        if not value:
            continue

        key = value.casefold()
        if key in seen:
            continue

        seen.add(key)
        out.append(value)

    return out


def compact_dict(data):
    """Remove only empty/null values; keep useful zeroes and False values."""
    return {
        key: value
        for key, value in data.items()
        if value not in (None, "", [], {})
    }


# ---------------------------------------------------------------------
# Folder-name handling and cautious title matching
# ---------------------------------------------------------------------

def norm_text(value):
    """Make title comparisons forgiving without making them fuzzy nonsense."""
    if not value:
        return ""

    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def clean_year(value):
    if value is None:
        return None

    match = re.search(r"(?:18|19|20)\d{2}", str(value))
    return int(match.group(0)) if match else None


def parse_title_and_year(raw):
    """Extract only a terminal year; leave numeric titles such as 1923 intact."""
    raw = raw.strip()

    bracketed = re.match(
        r"^(?P<title>.+?)\s*[\[(](?P<year>(?:18|19|20)\d{2})[\])]\s*$",
        raw,
    )
    if bracketed:
        return bracketed.group("title").strip(), int(bracketed.group("year"))

    bare = re.match(
        r"^(?P<title>.+?)\s+(?P<year>(?:18|19|20)\d{2})\s*$",
        raw,
    )
    if bare:
        return bare.group("title").strip(), int(bare.group("year"))

    return raw, None


def parse_folder_title(folder_name):
    """
    Split the library's usual seasonal-folder spelling into a show title, an
    optional premiere-year hint, and a local season number.

      Fallout - Season 01          -> ("Fallout", None, 1)
      Fallout (2024) - Season 1    -> ("Fallout", 2024, 1)
      Fallout - Season 1 [2024]    -> ("Fallout", 2024, 1)
      Fallout (2024)               -> ("Fallout", 2024, None)
      1923                         -> ("1923", None, None)

    The season suffix is anchored at the end.  A genuine title containing the
    word 'Season' remains untouched unless it really ends in '- Season N'.
    """
    raw = folder_name.strip()

    season_match = re.match(
        r"^(?P<title>.+?)\s*(?:-|–|—)\s*season\s*(?P<season>\d+)"
        r"(?:\s*[\[(](?P<trailing_year>(?:18|19|20)\d{2})[\])])?\s*$",
        raw,
        flags=re.IGNORECASE,
    )

    if season_match:
        title, embedded_year = parse_title_and_year(season_match.group("title"))
        trailing_year = season_match.group("trailing_year")
        year = int(trailing_year) if trailing_year else embedded_year
        return title, year, int(season_match.group("season"))

    title, year = parse_title_and_year(raw)
    return title, year, None


def tvdb_candidate_names(candidate):
    values = [
        candidate.get("name"),
        candidate.get("title"),
        candidate.get("name_translated"),
    ]
    values.extend(candidate.get("aliases") or [])
    return unique_strings(values)


def score_tvdb_match(query_title, query_year, candidate):
    """Return (score, exact-title-boolean) without trusting TVDB's score."""
    wanted = norm_text(query_title)
    got = [norm_text(name) for name in tvdb_candidate_names(candidate)]
    got = [name for name in got if name]

    exact = bool(wanted and any(wanted == name for name in got))
    loose = bool(wanted and any(wanted in name or name in wanted for name in got))

    score = 0.0
    if exact:
        score += 80.0
    elif loose:
        score += 35.0

    candidate_year = clean_year(
        candidate.get("year") or candidate.get("first_air_time")
    )

    if query_year and candidate_year:
        if candidate_year == query_year:
            score += 35.0
        elif abs(candidate_year - query_year) == 1:
            score += 15.0

    return score, exact


def tvdb_candidate_id(candidate):
    value = candidate.get("tvdb_id") or candidate.get("id") or candidate.get("objectID")
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def candidate_summary(score, exact, candidate):
    return compact_dict({
        "score": round(score, 2),
        "exact_title": exact,
        "tvdb_id": tvdb_candidate_id(candidate),
        "name": candidate.get("name") or candidate.get("title"),
        "year": candidate.get("year") or clean_year(candidate.get("first_air_time")),
        "type": candidate.get("type"),
        "overview": candidate.get("overview"),
    })


# ---------------------------------------------------------------------
# TVDB v4 client: login in memory, raw GET cache on disk
# ---------------------------------------------------------------------

class TVDBClient:
    """
    Small standard-library client for the TVDB v4 shape:

      POST /login  {"apikey": ..., optional "pin": ...}
      GET  /search?query=...&type=series
      GET  /series/{id}/extended

    The bearer token stays only in memory.  The durable raw cache holds GET
    response data only, never the key, PIN, or bearer token.
    """

    def __init__(self, api_key=None, pin=None, cache_file="tvdb_api_cache.json", verbose=True):
        self.api_key = api_key or os.environ.get("TVDB_API_KEY")
        self.pin = pin if pin is not None else os.environ.get("TVDB_PIN")
        self.cache_file = Path(cache_file)
        self.verbose = verbose
        self.cache = json_load(self.cache_file, {})
        self.token = None

        if not self.api_key:
            raise ValueError(
                "Set TVDB_API_KEY, export it in the shell, or fill TVDB_API_KEY "
                "near the top of this script."
            )

    def save_cache(self):
        json_save_atomic(self.cache_file, self.cache)

    def _url(self, path, params=None):
        url = TVDB_API_BASE.rstrip("/") + "/" + path.lstrip("/")
        if params:
            clean = {
                key: value
                for key, value in params.items()
                if value is not None and value != ""
            }
            if clean:
                url += "?" + urllib.parse.urlencode(sorted(clean.items()), doseq=True)
        return url

    def _request(self, method, path, params=None, payload=None, authenticated=True):
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")

        if authenticated:
            if not self.token:
                self.login()
            headers["Authorization"] = f"Bearer {self.token}"

        request = urllib.request.Request(
            self._url(path, params),
            data=data,
            headers=headers,
            method=method,
        )

        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            body = response.read().decode("utf-8")

        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"TVDB returned non-JSON data for {method} {path}: {e!r}") from e

    def login(self):
        payload = {"apikey": self.api_key}
        if self.pin:
            payload["pin"] = self.pin

        try:
            response = self._request("POST", "/login", payload=payload, authenticated=False)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"TVDB login failed: HTTP {e.code} {e.reason}") from e

        token = ((response.get("data") or {}).get("token") or "").strip()
        if not token:
            raise RuntimeError("TVDB login returned no bearer token")

        self.token = token
        if self.verbose:
            print("      TVDB login: bearer token acquired")

    def get(self, path, params=None):
        params = dict(params or {})
        cache_key = path + "?" + urllib.parse.urlencode(sorted(params.items()), doseq=True)

        if cache_key in self.cache:
            return self.cache[cache_key], True

        last_error = None
        refreshed_token = False

        for attempt in range(REQUEST_ATTEMPTS):
            try:
                response = self._request("GET", path, params=params, authenticated=True)
                data = response.get("data")

                # Store exactly the useful API payload.  No bearer token or
                # envelope state ends up in the persistent cache.
                self.cache[cache_key] = data
                self.save_cache()
                return data, False

            except urllib.error.HTTPError as e:
                last_error = e

                if e.code == 401 and not refreshed_token:
                    self.token = None
                    refreshed_token = True
                    if self.verbose:
                        print("      TVDB 401; refreshing bearer token once")
                    continue

                if e.code == 404:
                    self.cache[cache_key] = {}
                    self.save_cache()
                    return {}, False

                if e.code == 429 or 500 <= e.code < 600:
                    retry_after = e.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else min(2 ** attempt, 15)
                    except ValueError:
                        delay = min(2 ** attempt, 15)

                    if self.verbose:
                        print(f"      TVDB HTTP {e.code}; retrying in {delay:.1f}s")
                    time.sleep(delay)
                    continue

                raise RuntimeError(f"TVDB HTTP {e.code}: {e.reason}") from e

            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_error = e
                delay = min(2 ** attempt, 15)
                if self.verbose:
                    print(f"      TVDB network wobble; retrying in {delay:.1f}s: {e!r}")
                time.sleep(delay)

        raise RuntimeError(f"TVDB request failed after retries: {last_error!r}")


# ---------------------------------------------------------------------
# TVDB resolution and NFO/asset preparation
# ---------------------------------------------------------------------

def resolve_tvdb_series(tvdb, query_title, query_year):
    params = {
        "query": query_title,
        "type": "series",
        "limit": 10,
    }
    if query_year:
        params["year"] = query_year

    data, cache_hit = tvdb.get("/search", params)
    results = data or []

    if not isinstance(results, list):
        results = []

    # The type filter is requested from TVDB, but retain this guard so an odd
    # response cannot turn a person/movie with the same title into a TV show.
    results = [
        item for item in results
        if isinstance(item, dict)
        and str(item.get("type") or "series").casefold() == "series"
        and tvdb_candidate_id(item) is not None
    ]

    if not results:
        return {
            "ok": False,
            "status": "not_found",
            "cache_hit": cache_hit,
            "candidates": [],
        }

    scored = []
    for candidate in results:
        score, exact = score_tvdb_match(query_title, query_year, candidate)
        scored.append((score, exact, candidate))

    scored.sort(key=lambda item: item[0], reverse=True)

    best_score, best_exact, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    exact_count = sum(1 for _, exact, _ in scored if exact)
    candidate_year = clean_year(best.get("year") or best.get("first_air_time"))

    accepted = False
    if query_year:
        if best_exact and candidate_year == query_year:
            accepted = True
        elif best_score >= 100 and (len(scored) == 1 or best_score - second_score >= 20):
            accepted = True
    else:
        # One exact title is enough.  Multiple exact-title candidates (The
        # Office, Wilfred, etc.) remain unresolved on purpose.
        if best_exact and exact_count == 1:
            accepted = True

    candidate_list = [
        candidate_summary(score, exact, candidate)
        for score, exact, candidate in scored[:8]
    ]

    if not accepted:
        return {
            "ok": False,
            "status": "ambiguous",
            "cache_hit": cache_hit,
            "candidates": candidate_list,
        }

    return {
        "ok": True,
        "tvdb_id": tvdb_candidate_id(best),
        "selected": candidate_summary(best_score, best_exact, best),
        "cache_hit": cache_hit,
        "candidates": candidate_list,
    }


def absolute_tvdb_image_url(value):
    """TVDB generally returns full artwork URLs; safely accept relative ones too."""
    if not value:
        return None

    value = str(value).strip()
    if not value:
        return None

    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("/"):
        return TVDB_ARTWORK_BASE.rstrip("/") + value
    return TVDB_ARTWORK_BASE.rstrip("/") + "/" + value


def remote_id_for(remote_ids, wanted):
    wanted = wanted.casefold()

    for item in remote_ids or []:
        if not isinstance(item, dict):
            continue

        source = str(item.get("sourceName") or "").casefold()
        value = str(item.get("id") or "").strip()
        if value and wanted in source:
            return value

    if wanted == "imdb":
        for item in remote_ids or []:
            value = str((item or {}).get("id") or "").strip()
            if value.startswith("tt"):
                return value

    return None


def choose_tvdb_certification(content_ratings):
    preferred = ("usa", "us", "gbr", "gb", "can", "ca", "aus", "au", "nzl", "nz")
    rows = [item for item in (content_ratings or []) if isinstance(item, dict)]

    selected = None
    for country in preferred:
        for item in rows:
            if str(item.get("country") or "").casefold() == country:
                if item.get("name") or item.get("fullName"):
                    selected = item
                    break
        if selected:
            break

    if not selected:
        selected = next(
            (item for item in rows if item.get("name") or item.get("fullName")),
            None,
        )

    if not selected:
        return None, None

    rating = str(selected.get("name") or selected.get("fullName") or "").strip()
    full = str(selected.get("fullName") or "").strip()
    country = str(selected.get("country") or "").strip()
    certification = full or (f"{country}:{rating}" if country else rating)
    return rating or None, certification or None


def choose_poster_url(details):
    # TVDB's main series image is normally its preferred poster.  Do not turn
    # artwork-type IDs into a dependency unless the ordinary answer is absent.
    direct = absolute_tvdb_image_url(details.get("image"))
    if direct:
        return direct

    artworks = [item for item in (details.get("artworks") or []) if isinstance(item, dict)]
    if not artworks:
        return None

    def artwork_rank(item):
        width = item.get("width") or 0
        height = item.get("height") or 0
        portrait = bool(width and height and width / height < 0.86)
        try:
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        return (portrait, bool(item.get("includesText")), score, height, width)

    artworks.sort(key=artwork_rank, reverse=True)
    return absolute_tvdb_image_url(artworks[0].get("image"))


def build_actor_lists(characters):
    """
    TVDB gives show-level character records carrying personName, role name, a
    sort order, and personImgURL.  That means the actual known-show cast is
    available directly—no global person search or one-actor-at-a-time crawl.
    """
    rows = [item for item in (characters or []) if isinstance(item, dict)]
    rows.sort(key=lambda item: item.get("sort", 999999))

    nfo_actors = []
    actor_urls = []
    seen_names = set()

    for index, person in enumerate(rows):
        name = str(person.get("personName") or "").strip()
        if not name:
            continue

        key = norm_text(name)
        if not key or key in seen_names:
            continue
        seen_names.add(key)

        role = str(person.get("name") or "").strip() or None
        order = person.get("sort")
        if not isinstance(order, int):
            order = index

        nfo_actors.append(compact_dict({
            "name": name,
            "role": role,
            "order": order,
        }))

        photo_url = absolute_tvdb_image_url(person.get("personImgURL"))
        if photo_url:
            actor_urls.append({"name": name, "url": photo_url})

    return nfo_actors, actor_urls


def selected_seasons(details):
    """Return the default-order season inventory without episode archaeology."""
    default_type = details.get("defaultSeasonType")
    all_seasons = [item for item in (details.get("seasons") or []) if isinstance(item, dict)]

    chosen = []
    for season in all_seasons:
        season_type = season.get("type") or {}
        type_id = season_type.get("id") if isinstance(season_type, dict) else None

        if default_type is not None and type_id is not None and type_id != default_type:
            continue

        chosen.append(compact_dict({
            "season_number": season.get("number"),
            "name": season.get("name"),
            "year": clean_year(season.get("year")),
            "season_type": season_type.get("name") if isinstance(season_type, dict) else None,
        }))

    chosen.sort(key=lambda item: (item.get("season_number") is None, item.get("season_number", 999999)))
    return chosen


def build_nfo_payload(details):
    """Build rich link-free NFO metadata plus a separate acquisition section."""
    tvdb_id = details.get("id")
    if not tvdb_id:
        raise ValueError("TVDB extended series record has no id")

    remote_ids = details.get("remoteIds") or []
    imdb_id = remote_id_for(remote_ids, "imdb")
    tmdb_id = remote_id_for(remote_ids, "tmdb") or remote_id_for(remote_ids, "themoviedb")
    wikidata_id = remote_id_for(remote_ids, "wikidata")

    ids = {"tvdb": tvdb_id}
    if imdb_id:
        ids["imdb"] = imdb_id
    if tmdb_id:
        ids["tmdb"] = tmdb_id
    if wikidata_id:
        ids["wikidata"] = wikidata_id

    aliases = unique_strings(
        item.get("name")
        for item in (details.get("aliases") or [])
        if isinstance(item, dict)
    )

    countries = unique_strings([
        details.get("originalCountry"),
        details.get("country"),
    ])

    languages = unique_strings([
        details.get("originalLanguage"),
    ])

    mpaa, certification = choose_tvdb_certification(details.get("contentRatings"))
    nfo_actors, actor_urls = build_actor_lists(details.get("characters"))
    seasons = selected_seasons(details)

    air_days = []
    for day, enabled in (details.get("airsDays") or {}).items():
        if enabled:
            air_days.append(day.capitalize())

    networks = unique_strings([
        ((details.get("originalNetwork") or {}).get("name")),
        ((details.get("latestNetwork") or {}).get("name")),
    ])

    studios = unique_strings(
        item.get("name")
        for item in (details.get("companies") or [])
        if isinstance(item, dict)
    )

    tags = unique_strings(
        item.get("name")
        for item in (details.get("tags") or [])
        if isinstance(item, dict)
    )

    # This count deliberately uses the selected/default season order.  Season
    # zero remains real metadata but does not inflate the ordinary season count.
    ordinary_seasons = [
        item for item in seasons
        if isinstance(item.get("season_number"), int) and item["season_number"] > 0
    ]

    nfo = compact_dict({
        "title": details.get("name"),
        "originaltitle": details.get("name"),
        "sorttitle": details.get("name"),
        "alternativetitles": aliases,
        # TVDB's generic 'score' is explicitly not promised to be a user
        # rating, so do not pretend it is one in <rating>.
        "year": clean_year(details.get("year") or details.get("firstAired")),
        "outline": details.get("overview"),
        "plot": details.get("overview"),
        "runtime": details.get("averageRuntime"),
        "status": ((details.get("status") or {}).get("name")),
        "abbreviation": details.get("abbreviation"),
        "mpaa": mpaa,
        "certification": certification,
        "id": imdb_id or str(tvdb_id),
        "ids": ids,
        "tmdbId": tmdb_id,
        "tvdbId": tvdb_id,
        "country": [item.lower() for item in countries],
        "originalcountry": details.get("originalCountry"),
        "premiered": details.get("firstAired"),
        "lastaired": details.get("lastAired"),
        "nextaired": details.get("nextAired"),
        "airstime": details.get("airsTime"),
        "airsday": air_days,
        "originallanguage": details.get("originalLanguage"),
        "language": languages,
        "genre": unique_strings(
            item.get("name")
            for item in (details.get("genres") or [])
            if isinstance(item, dict)
        ),
        "keyword": tags,
        "studio": studios,
        "network": networks,
        "defaultseasontype": details.get("defaultSeasonType"),
        "isorderrandomized": details.get("isOrderRandomized"),
        "numberofseasons": len(ordinary_seasons) if ordinary_seasons else None,
        "seasons": seasons,
        "actor": nfo_actors,
    })

    assets = compact_dict({
        "poster_url": choose_poster_url(details),
        "actor_urls": actor_urls,
    })

    return nfo, assets


# ---------------------------------------------------------------------
# Manifest / work-list management
# ---------------------------------------------------------------------

def new_record(show_dir):
    title, year, season = parse_folder_title(show_dir.name)
    return {
        "folder_name": show_dir.name,
        "query_title": title,
        "query_year": year,
        "local_season": season,
        "status": "pending",
        "tries": 0,
        "updated": None,
        "last_error": None,
        "tvdb_id": None,
        "match": None,
        "candidates": [],
        "nfo": None,
        "assets": None,
    }


def new_manifest(library_root):
    return {
        "_meta": {
            "version": 2,
            "source": "TheTVDB v4",
            "created": now_iso(),
            "updated": now_iso(),
            "library_root": str(library_root),
            "notes": (
                "Stage 1 TVDB work file. NFO metadata is URL-free; poster and "
                "actor image URLs live only in each record's assets section."
            ),
        },
        "shows": {},
    }


def scan_immediate_show_dirs(library_root):
    if not library_root.is_dir():
        raise FileNotFoundError(f"TV_SHOWS_DIR is not a directory: {library_root}")

    # The library root itself is the only scan boundary.  A Season 01 folder
    # below a show folder cannot accidentally become a show record.
    return sorted(
        [
            child.resolve()
            for child in library_root.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        ],
        key=lambda path: path.name.casefold(),
    )


def load_or_merge_manifest(library_root, work_path, rebuild=False):
    dirs = scan_immediate_show_dirs(library_root)

    if rebuild:
        manifest = new_manifest(library_root)
    else:
        manifest = json_load(work_path, None)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("shows"), dict):
            manifest = new_manifest(library_root)

    manifest.setdefault("_meta", {})
    manifest["_meta"].setdefault("version", 2)
    manifest["_meta"]["source"] = "TheTVDB v4"
    manifest["_meta"]["library_root"] = str(library_root)
    manifest.setdefault("shows", {})

    records = manifest["shows"]
    added = 0

    for show_dir in dirs:
        key = str(show_dir)
        if key not in records:
            records[key] = new_record(show_dir)
            added += 1
            continue

        # Keep past resolution/acquisition state, but refresh the spelling and
        # parser hints in case the title folder was renamed between runs.
        title, year, season = parse_folder_title(show_dir.name)
        record = records[key]
        record["folder_name"] = show_dir.name
        record["query_title"] = title
        record["query_year"] = year
        record["local_season"] = season

    # Intentionally retain records for absent folders.  A temporarily
    # unmounted drive must not erase slow-gathered metadata.
    manifest["_meta"]["updated"] = now_iso()
    json_save_atomic(work_path, manifest)
    return manifest, dirs, added


def manifest_status_counts(manifest):
    counts = Counter(
        record.get("status", "unknown")
        for record in manifest.get("shows", {}).values()
    )
    return dict(sorted(counts.items()))


def should_process(record):
    status = record.get("status", "pending")
    if status == "pending":
        return True
    if status == "matched":
        return REFRESH_MATCHED
    if status == "error":
        return RETRY_ERRORS
    if status == "ambiguous":
        return RETRY_AMBIGUOUS
    if status == "not_found":
        return RETRY_NOT_FOUND
    return False


def short_candidates(candidates, count=3):
    out = []
    for item in (candidates or [])[:count]:
        title = item.get("name") or "?"
        year = item.get("year") or "?"
        out.append(f"{title} ({year})")
    return "; ".join(out) if out else "no usable candidates"


# ---------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------

def scan_tv_shows():
    here = script_dir()
    library_root = Path(TV_SHOWS_DIR).expanduser().resolve()
    work_path = here / WORK_FILE
    cache_path = here / TVDB_CACHE_FILE

    manifest, dirs, added = load_or_merge_manifest(
        library_root,
        work_path,
        rebuild=REBUILD_WORK_FILE,
    )

    tvdb = TVDBClient(
        api_key=TVDB_API_KEY or None,
        pin=TVDB_PIN or None,
        cache_file=cache_path,
        verbose=VERBOSE,
    )

    records = manifest["shows"]
    work_items = [
        (str(show_dir), records[str(show_dir)])
        for show_dir in dirs
        if should_process(records[str(show_dir)])
    ]

    print("TV TVDB URL SCANNER")
    print("-------------------")
    print("Library root:", library_root)
    print("Immediate title folders:", len(dirs))
    print("New work records:", added)
    print("Status before:", manifest_status_counts(manifest))
    print("Resolvable this run:", len(work_items))
    print("Work file:", work_path)
    print("Raw API cache:", cache_path)
    print()

    processed = matched = ambiguous = not_found = errors = 0
    api_warm = api_cold = 0
    changed_since_save = 0
    start = time.time()

    try:
        for path, record in work_items:
            if LIMIT is not None and processed >= LIMIT:
                break

            folder_name = record.get("folder_name") or Path(path).name
            title = record.get("query_title") or folder_name
            year = record.get("query_year")
            season = record.get("local_season")
            old_status = record.get("status", "pending")

            processed += 1
            record["tries"] = int(record.get("tries") or 0) + 1
            record["updated"] = now_iso()
            record["last_error"] = None

            display_query = f"{title} ({year})" if year else title
            if season is not None:
                display_query += f"  [local season {int(season):02d}]"

            print(f"[{processed:04d}/{len(work_items):04d}] LOOK  {folder_name}")
            print(f"             QUERY  {display_query}")

            try:
                resolved = resolve_tvdb_series(tvdb, title, year)
                if resolved.get("cache_hit"):
                    api_warm += 1
                else:
                    api_cold += 1

                if not resolved.get("ok"):
                    # An explicit refresh must not demolish an already useful
                    # record just because a fresh search now looks ambiguous.
                    if old_status == "matched" and record.get("nfo"):
                        record["last_error"] = (
                            f"refresh left unresolved: {resolved.get('status')}"
                        )
                        print(f"             KEEP MATCH  {display_query} ({resolved.get('status')})")
                    else:
                        record["status"] = resolved["status"]
                        record["candidates"] = resolved.get("candidates") or []
                        record["match"] = None
                        record["tvdb_id"] = None
                        record["nfo"] = None
                        record["assets"] = None

                        if resolved["status"] == "ambiguous":
                            ambiguous += 1
                            print("             AMBIG  " + short_candidates(record["candidates"]))
                        else:
                            not_found += 1
                            print("             MISS   no TVDB series result")
                    continue

                tvdb_id = resolved["tvdb_id"]
                details, detail_cache_hit = tvdb.get(f"/series/{tvdb_id}/extended", {})
                if detail_cache_hit:
                    api_warm += 1
                else:
                    api_cold += 1

                if not isinstance(details, dict) or not details.get("id"):
                    raise RuntimeError("TVDB returned no usable extended series payload")

                nfo, assets = build_nfo_payload(details)

                record["status"] = "matched"
                record["tvdb_id"] = tvdb_id
                record["match"] = {
                    "method": "folder_title_search",
                    "query_title": title,
                    "query_year": year,
                    "local_season": season,
                    "selected": resolved["selected"],
                }
                record["candidates"] = resolved.get("candidates") or []
                record["nfo"] = nfo
                record["assets"] = assets
                record["last_error"] = None

                matched += 1
                actor_count = len(nfo.get("actor") or [])
                face_count = len(assets.get("actor_urls") or [])
                poster = "yes" if assets.get("poster_url") else "no"
                cache_note = "warm" if detail_cache_hit and resolved.get("cache_hit") else "mixed"

                print(
                    f"             MATCH  TVDB {tvdb_id}  "
                    f"cast={actor_count} faces={face_count} poster={poster} "
                    f"cache={cache_note}"
                )

            except KeyboardInterrupt:
                raise

            except Exception as e:
                # A failed refresh should leave old material usable.
                if old_status == "matched" and record.get("nfo"):
                    record["status"] = "matched"
                    record["last_error"] = repr(e)
                    print(f"             KEEP MATCH  refresh error: {e!r}")
                else:
                    record["status"] = "error"
                    record["last_error"] = repr(e)
                    errors += 1
                    print(f"             ERROR  {e!r}")

            finally:
                changed_since_save += 1
                manifest["_meta"]["updated"] = now_iso()
                if changed_since_save >= SAVE_EVERY_SHOWS:
                    json_save_atomic(work_path, manifest)
                    changed_since_save = 0

            if SLEEP_BETWEEN_SHOWS:
                time.sleep(SLEEP_BETWEEN_SHOWS)

    except KeyboardInterrupt:
        print("\nCTRL-C: preserving the current work file and raw GET cache...")

    finally:
        manifest["_meta"]["updated"] = now_iso()
        manifest["_meta"]["last_run"] = {
            "finished": now_iso(),
            "processed": processed,
            "matched": matched,
            "ambiguous": ambiguous,
            "not_found": not_found,
            "errors": errors,
            "api_cache_hits": api_warm,
            "api_cache_misses": api_cold,
        }
        json_save_atomic(work_path, manifest)
        tvdb.save_cache()

    elapsed = time.time() - start
    print()
    print("SCAN STOPPED" if processed < len(work_items) and LIMIT is None else "SCAN COMPLETE")
    print("-------------")
    print("Processed this run:", processed)
    print("Matched this run:", matched)
    print("Ambiguous this run:", ambiguous)
    print("Not found this run:", not_found)
    print("Errors this run:", errors)
    print("API cache: warm=%d cold=%d" % (api_warm, api_cold))
    print("Status after:", manifest_status_counts(manifest))
    print("Elapsed:", f"{elapsed:.1f}s")
    if elapsed > 0 and processed:
        print("Rate:", f"{processed / elapsed:.2f} title folders/sec")

    return manifest


if __name__ == "__main__":
    scan_tv_shows()
