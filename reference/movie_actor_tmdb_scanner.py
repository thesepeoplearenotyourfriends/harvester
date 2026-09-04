import os
import re
import json
import time
import urllib.parse
import urllib.request
import urllib.error
import unicodedata
import xml.etree.ElementTree as et

from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict


TMDB_API_BASE = "https://api.themoviedb.org/3"


# ---------------------------------------------------------------------
# Basic persistence helpers
# ---------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def json_save_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = str(path) + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)

    os.replace(tmp, path)


def script_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return os.getcwd()


# ---------------------------------------------------------------------
# Text cleanup / matching helpers
# ---------------------------------------------------------------------

def norm_text(s):
    if not s:
        return ""

    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def clean_year(s):
    if not s:
        return None

    m = re.search(r"(19|20)\d{2}", str(s))

    if not m:
        return None

    return int(m.group(0))


def valid_http_url(url):
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def add_url(db, name, url, max_urls=None):
    if not name or not url:
        return

    db.setdefault(name, [])

    if url in db[name]:
        return

    if max_urls is not None and len(db[name]) >= max_urls:
        return

    db[name].append(url)


# ---------------------------------------------------------------------
# TMDB client with persistent raw API cache
# ---------------------------------------------------------------------

class TMDBClient:
    def __init__(
        self,
        api_key=None,
        bearer_token=None,
        cache_file="tmdb_api_cache.json",
        language="en-US",
        verbose=True,
    ):
        self.api_key = api_key or os.environ.get("TMDB_API_KEY")
        self.bearer_token = bearer_token or os.environ.get("TMDB_BEARER_TOKEN")
        self.cache_file = cache_file
        self.language = language
        self.verbose = verbose
        self.cache = json_load(cache_file, {})

        if not self.api_key and not self.bearer_token:
            raise ValueError(
                "Set TMDB_API_KEY, pass api_key=..., or set TMDB_BEARER_TOKEN."
            )

    def save_cache(self):
        json_save_atomic(self.cache_file, self.cache)

    def get(self, path, params=None):
        params = dict(params or {})

        if "language" not in params:
            params["language"] = self.language

        cache_params = {
            k: v for k, v in params.items()
            if k != "api_key"
        }

        cache_key = path + "?" + urllib.parse.urlencode(
            sorted(cache_params.items()),
            doseq=True,
        )

        if cache_key in self.cache:
            return self.cache[cache_key]

        request_params = dict(params)

        headers = {
            "Accept": "application/json",
            "User-Agent": "local-nfo-tmdb-thumb-cache/1.0",
        }

        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        else:
            request_params["api_key"] = self.api_key

        url = TMDB_API_BASE + path

        if request_params:
            url += "?" + urllib.parse.urlencode(request_params, doseq=True)

        for attempt in range(5):
            req = urllib.request.Request(url, headers=headers)

            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                self.cache[cache_key] = data
                self.save_cache()
                return data

            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry_after = e.headers.get("Retry-After")
                    delay = int(retry_after) if retry_after and retry_after.isdigit() else 0.3

                    if self.verbose:
                        print(f"TMDB 429; sleeping {delay}s")

                    time.sleep(delay)
                    continue

                if e.code == 404:
                    data = {}
                    self.cache[cache_key] = data
                    self.save_cache()
                    return data

                raise

            except Exception:
                if attempt == 4:
                    raise

                time.sleep(attempt)

        return {}


# ---------------------------------------------------------------------
# NFO parsing
# ---------------------------------------------------------------------

def first_text(root, *tags):
    for tag in tags:
        value = root.findtext(tag)

        if value:
            return value.strip()

    return None


def uniqueid(root, wanted_type):
    wanted_type = wanted_type.lower()

    for node in root.findall("./uniqueid"):
        node_type = (node.attrib.get("type") or "").lower().strip()
        value = (node.text or "").strip()

        if node_type == wanted_type and value:
            return value

    return None


def parse_nfo_file(path):
    try:
        tree = et.parse(path)
    except Exception:
        return None

    root = tree.getroot()

    title = first_text(root, "title", "originaltitle")
    original_title = first_text(root, "originaltitle")

    year = clean_year(
        first_text(root, "year", "premiered", "releasedate", "aired")
    )

    imdb_id = (
        uniqueid(root, "imdb")
        or first_text(root, "imdbid")
    )

    generic_id = first_text(root, "id")

    if generic_id and generic_id.startswith("tt") and not imdb_id:
        imdb_id = generic_id

    tmdb_id = (
        uniqueid(root, "tmdb")
        or first_text(root, "tmdbid")
    )

    if tmdb_id and not str(tmdb_id).isdigit():
        tmdb_id = None

    actors = []

    for actor_node in root.findall(".//actor"):
        name = actor_node.findtext("name")
        role = actor_node.findtext("role")
        thumb = actor_node.findtext("thumb")

        if not name:
            continue

        actors.append({
            "name": name.strip(),
            "role": role.strip() if role else None,
            "nfo_thumb": thumb.strip() if thumb else None,
        })

    if not actors:
        return None

    return {
        "path": os.path.abspath(path),
        "title": title,
        "original_title": original_title,
        "year": year,
        "imdb_id": imdb_id,
        "tmdb_id": int(tmdb_id) if tmdb_id and str(tmdb_id).isdigit() else None,
        "actors": actors,
    }


def scan_nfos(DIRS):
    records = []

    for base_dir in DIRS:
        for root, dirs, files in os.walk(base_dir):
            for file in files:
                if not file.lower().endswith(".nfo"):
                    continue

                full = os.path.join(root, file)
                rec = parse_nfo_file(full)

                if rec:
                    records.append(rec)

    return records


# ---------------------------------------------------------------------
# Durable work queue
# ---------------------------------------------------------------------

def make_actor_work_queue(DIRS, queue_file, force_rebuild=False):
    """
    Expensive phase.

    Walks NFO files once and builds:

      actor name -> movie contexts where that actor appears

    Future runs should reuse actor_thumb_work_queue.json instead of
    walking all NFOs again.
    """

    if not force_rebuild and os.path.exists(queue_file):
        data = json_load(queue_file, None)

        if isinstance(data, dict) and "actors" in data:
            return data

    nfos = scan_nfos(DIRS)

    actors = defaultdict(lambda: {
        "status": "pending",
        "tries": 0,
        "updated": None,
        "urls": [],
        "tmdb_person_id": None,
        "tmdb_person_name": None,
        "matched_context": None,
        "movie_tmdb_id": None,
        "fail_reason": None,
        "failures": [],
        "contexts": [],
    })

    for nfo in nfos:
        for actor in nfo["actors"]:
            name = actor["name"]

            actors[name]["contexts"].append({
                "title": nfo.get("title"),
                "original_title": nfo.get("original_title"),
                "year": nfo.get("year"),
                "imdb_id": nfo.get("imdb_id"),
                "tmdb_id": nfo.get("tmdb_id"),
                "nfo": nfo.get("path"),
                "role": actor.get("role"),
                "old_nfo_thumb": actor.get("nfo_thumb"),
            })

    queue = {
        "_meta": {
            "version": 1,
            "created": now_iso(),
            "updated": now_iso(),
            "source_dirs": [os.path.abspath(x) for x in DIRS],
            "nfo_count": len(nfos),
            "actor_count": len(actors),
        },
        "actors": dict(sorted(actors.items())),
    }

    json_save_atomic(queue_file, queue)
    return queue


def queue_stats(queue):
    stats = defaultdict(int)

    for item in queue.get("actors", {}).values():
        stats[item.get("status", "unknown")] += 1

    return dict(sorted(stats.items()))


def write_final_actor_db_from_queue(queue, cache_file):
    """
    Writes the simple final product:

      {
        "Actor Name": ["https://image.tmdb.org/t/p/w185/....jpg"]
      }
    """

    actor_thumbs = {}

    for name, item in queue.get("actors", {}).items():
        urls = item.get("urls") or []

        if urls:
            actor_thumbs[name] = urls

    json_save_atomic(cache_file, actor_thumbs)
    return actor_thumbs


# ---------------------------------------------------------------------
# TMDB movie + actor resolution
# ---------------------------------------------------------------------

def score_movie_match(nfo, movie):
    wanted_titles = [
        norm_text(nfo.get("title")),
        norm_text(nfo.get("original_title")),
    ]

    wanted_titles = [x for x in wanted_titles if x]

    got_titles = [
        norm_text(movie.get("title")),
        norm_text(movie.get("original_title")),
    ]

    got_titles = [x for x in got_titles if x]

    score = 0

    exact_title = any(w == g for w in wanted_titles for g in got_titles)
    loose_title = any((w in g or g in w) for w in wanted_titles for g in got_titles)

    if exact_title:
        score += 80
    elif loose_title:
        score += 35

    wanted_year = nfo.get("year")
    got_year = clean_year(movie.get("release_date"))

    if wanted_year and got_year:
        if wanted_year == got_year:
            score += 35
        elif abs(wanted_year - got_year) == 1:
            score += 15

    # Tiny tie-breaker only. Popularity should not override title/year sanity.
    try:
        score += min(float(movie.get("popularity") or 0.0), 50.0) / 10.0
    except Exception:
        pass

    return score


def resolve_movie_tmdb_id(tmdb, nfo):
    """
    Best path:

      local TMDB id
      IMDb id through /find
      title/year search as fallback
    """

    if nfo.get("tmdb_id"):
        return {
            "ok": True,
            "movie_id": nfo["tmdb_id"],
            "method": "local_tmdb_id",
        }

    imdb_id = nfo.get("imdb_id")

    if imdb_id and str(imdb_id).startswith("tt"):
        data = tmdb.get(
            f"/find/{imdb_id}",
            {
                "external_source": "imdb_id",
            },
        )

        movie_results = data.get("movie_results") or []

        if movie_results:
            return {
                "ok": True,
                "movie_id": movie_results[0].get("id"),
                "method": "imdb_find",
            }

    title = nfo.get("title")

    if not title:
        return {
            "ok": False,
            "reason": "no_title_or_id",
        }

    params = {
        "query": title,
        "include_adult": "false",
        "page": 1,
    }

    if nfo.get("year"):
        params["year"] = nfo["year"]
        params["primary_release_year"] = nfo["year"]

    data = tmdb.get("/search/movie", params)
    results = data.get("results") or []

    if not results:
        return {
            "ok": False,
            "reason": "no_movie_search_results",
            "title": title,
            "year": nfo.get("year"),
        }

    scored = sorted(
        ((score_movie_match(nfo, movie), movie) for movie in results),
        key=lambda x: x[0],
        reverse=True,
    )

    best_score, best_movie = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0

    if best_score >= 100 or (best_score >= 85 and best_score - second_score >= 20):
        return {
            "ok": True,
            "movie_id": best_movie.get("id"),
            "method": "title_year_search",
            "score": best_score,
        }

    return {
        "ok": False,
        "reason": "ambiguous_movie_search",
        "title": title,
        "year": nfo.get("year"),
        "top": [
            {
                "score": score,
                "id": movie.get("id"),
                "title": movie.get("title"),
                "original_title": movie.get("original_title"),
                "release_date": movie.get("release_date"),
            }
            for score, movie in scored[:5]
        ],
    }


def match_actor_in_credits(actor_name, credits):
    """
    Conservative by design.

    Exact normalized actor-name match only, within the already-resolved movie.
    This avoids global person-search roulette.
    """

    wanted = norm_text(actor_name)

    if not wanted:
        return None

    cast = credits.get("cast") or []
    exact = []

    for person in cast:
        names = [
            person.get("name"),
            person.get("original_name"),
        ]

        names = [norm_text(x) for x in names if x]

        if wanted in names:
            exact.append(person)

    if exact:
        return sorted(exact, key=lambda p: p.get("order", 999999))[0]

    return None


def get_tmdb_image_base(tmdb):
    config = tmdb.get("/configuration", {})
    images = config.get("images") or {}

    base = images.get("secure_base_url") or images.get("base_url")
    profile_sizes = images.get("profile_sizes") or []

    if not base:
        base = "https://image.tmdb.org/t/p/"

    return base, profile_sizes


def build_image_url(base, size, file_path):
    if not file_path:
        return None

    if not file_path.startswith("/"):
        file_path = "/" + file_path

    return base.rstrip("/") + "/" + size.strip("/") + file_path


def best_person_image_urls(tmdb, person_id, base, size, limit):
    if not person_id:
        return []

    data = tmdb.get(f"/person/{person_id}/images", {})
    profiles = data.get("profiles") or []

    profiles = sorted(
        profiles,
        key=lambda img: (
            img.get("vote_count") or 0,
            img.get("vote_average") or 0,
            img.get("width") or 0,
            img.get("height") or 0,
        ),
        reverse=True,
    )

    urls = []

    for img in profiles:
        url = build_image_url(base, size, img.get("file_path"))

        if url and url not in urls:
            urls.append(url)

        if limit is not None and len(urls) >= limit:
            break

    return urls


def resolve_actor_from_contexts(
    tmdb,
    actor_name,
    contexts,
    image_base,
    image_size,
    max_images_per_actor=1,
):
    """
    Resolve one actor using their known local movie contexts.

    Avoids:

      /search/person?query=Adam Cole

    Prefers:

      resolve local movie -> inspect that movie's credits -> match Adam Cole there
    """

    failures = []

    for ctx in contexts:
        fake_nfo = {
            "title": ctx.get("title"),
            "original_title": ctx.get("original_title"),
            "year": ctx.get("year"),
            "imdb_id": ctx.get("imdb_id"),
            "tmdb_id": ctx.get("tmdb_id"),
            "path": ctx.get("nfo"),
        }

        movie_result = resolve_movie_tmdb_id(tmdb, fake_nfo)

        if not movie_result.get("ok"):
            failures.append({
                "context": ctx,
                "reason": "movie_not_resolved",
                "movie_result": movie_result,
            })
            continue

        movie_id = movie_result.get("movie_id")

        credits = tmdb.get(f"/movie/{movie_id}/credits", {})
        cast_person = match_actor_in_credits(actor_name, credits)

        if not cast_person:
            failures.append({
                "context": ctx,
                "reason": "actor_not_in_movie_credits",
                "movie_tmdb_id": movie_id,
                "movie_method": movie_result.get("method"),
            })
            continue

        urls = []

        profile_url = build_image_url(
            image_base,
            image_size,
            cast_person.get("profile_path"),
        )

        if profile_url:
            urls.append(profile_url)

        if max_images_per_actor is None or len(urls) < max_images_per_actor:
            needed = (
                10
                if max_images_per_actor is None
                else max_images_per_actor - len(urls)
            )

            more_urls = best_person_image_urls(
                tmdb,
                cast_person.get("id"),
                image_base,
                image_size,
                needed,
            )

            for url in more_urls:
                if url and url not in urls:
                    urls.append(url)

        if max_images_per_actor is not None:
            urls = urls[:max_images_per_actor]

        if urls:
            return {
                "ok": True,
                "urls": urls,
                "tmdb_person_id": cast_person.get("id"),
                "tmdb_person_name": cast_person.get("name"),
                "matched_context": ctx,
                "movie_tmdb_id": movie_id,
                "movie_method": movie_result.get("method"),
            }

        failures.append({
            "context": ctx,
            "reason": "person_found_but_no_profile_image",
            "movie_tmdb_id": movie_id,
            "tmdb_person_id": cast_person.get("id"),
            "tmdb_person_name": cast_person.get("name"),
        })

    return {
        "ok": False,
        "fail_reason": "no_context_resolved_to_actor_image",
        "failures": failures[:10],
    }


# ---------------------------------------------------------------------
# Optional global person-search fallback
# ---------------------------------------------------------------------

def fallback_person_search(
    tmdb,
    name,
    contexts,
    image_base,
    image_size,
    max_images_per_actor=1,
):
    """
    Last-resort fallback.

    Off by default in collect_actor_thumb_urls(), because this is where
    mismatched same-name actors can creep in.

    It only accepts exact-name matches with a clear score gap.
    """

    data = tmdb.get(
        "/search/person",
        {
            "query": name,
            "include_adult": "false",
            "page": 1,
        },
    )

    results = data.get("results") or []

    if not results:
        return {
            "ok": False,
            "reason": "no_person_search_results",
        }

    local_titles = {
        norm_text(ctx.get("title"))
        for ctx in contexts
        if ctx.get("title")
    }

    wanted = norm_text(name)
    scored = []

    for person in results:
        score = 0

        if norm_text(person.get("name")) != wanted:
            continue

        score += 100

        if person.get("known_for_department") == "Acting":
            score += 5

        if person.get("profile_path"):
            score += 10

        for item in person.get("known_for") or []:
            known_title = norm_text(
                item.get("title")
                or item.get("name")
                or item.get("original_title")
                or item.get("original_name")
            )

            if known_title and known_title in local_titles:
                score += 40

        try:
            score += min(float(person.get("popularity") or 0.0), 50.0) / 10.0
        except Exception:
            pass

        scored.append((score, person))

    if not scored:
        return {
            "ok": False,
            "reason": "no_exact_name_person_results",
        }

    scored.sort(key=lambda x: x[0], reverse=True)

    best_score, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0

    if best_score < 115 or best_score - second_score < 20:
        return {
            "ok": False,
            "reason": "ambiguous_person_search",
            "top": [
                {
                    "score": score,
                    "id": person.get("id"),
                    "name": person.get("name"),
                    "known_for_department": person.get("known_for_department"),
                    "popularity": person.get("popularity"),
                    "known_for": [
                        item.get("title")
                        or item.get("name")
                        or item.get("original_title")
                        or item.get("original_name")
                        for item in (person.get("known_for") or [])[:5]
                    ],
                }
                for score, person in scored[:5]
            ],
        }

    urls = []

    profile_url = build_image_url(image_base, image_size, best.get("profile_path"))

    if profile_url:
        urls.append(profile_url)

    if max_images_per_actor is None or len(urls) < max_images_per_actor:
        needed = (
            10
            if max_images_per_actor is None
            else max_images_per_actor - len(urls)
        )

        more_urls = best_person_image_urls(
            tmdb,
            best.get("id"),
            image_base,
            image_size,
            needed,
        )

        for url in more_urls:
            if url and url not in urls:
                urls.append(url)

    if max_images_per_actor is not None:
        urls = urls[:max_images_per_actor]

    if not urls:
        return {
            "ok": False,
            "reason": "person_found_but_no_profile_image",
            "tmdb_person_id": best.get("id"),
            "tmdb_person_name": best.get("name"),
        }

    return {
        "ok": True,
        "urls": urls,
        "tmdb_person_id": best.get("id"),
        "tmdb_person_name": best.get("name"),
        "method": "person_search_fallback",
    }


# ---------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------

def collect_actor_thumb_urls(
    DIRS,
    api_key=None,
    bearer_token=None,
    rebuild_queue=False,
    refresh_ok=False,
    retry_failed=False,
    retry_error=True,
    allow_name_fallback=False,
    include_old_nfo_urls=False,
    cache_file=None,
    queue_file=None,
    tmdb_cache_file=None,
    image_size="w185",
    max_images_per_actor=1,
    save_every=50,
    limit=None,
    verbose=True,
):
    """
    Durable/resumable TMDB actor thumbnail collector.

    Normal behavior:
      - If actor_thumb_work_queue.json does not exist, scan NFOs once.
      - If it does exist, reuse it.
      - Skip actors already marked "ok".
      - Skip actors already marked "failed" unless retry_failed=True.
      - Retry actors marked "error" by default.
      - Save after every processed actor by default.

    Args:
      DIRS:
        List of media dirs to scan for .nfo files.

      api_key:
        TMDB v3 API key. Can also use env var TMDB_API_KEY.

      bearer_token:
        TMDB bearer token. Can also use env var TMDB_BEARER_TOKEN.

      rebuild_queue:
        Rewalk all .nfo files and rebuild actor_thumb_work_queue.json.

      refresh_ok:
        Reprocess actors already marked ok.

      retry_failed:
        Reprocess actors already marked failed.

      retry_error:
        Reprocess actors marked error.

      allow_name_fallback:
        Enable global /search/person fallback. Default False because this
        is where same-name mismatches can happen.

      include_old_nfo_urls:
        Also keep valid old NFO thumb URLs as fallback candidates.

      limit:
        Process only N actors this run. Useful for testing:
          limit=50

    Returns:
      Simple final actor -> urls dict.
    """

    here = script_dir()

    if cache_file is None:
        cache_file = os.path.join(here, "actor_thumb_urls_tmdb.json")

    if queue_file is None:
        queue_file = os.path.join(here, "actor_thumb_work_queue.json")

    if tmdb_cache_file is None:
        tmdb_cache_file = os.path.join(here, "tmdb_api_cache.json")

    queue = make_actor_work_queue(
        DIRS,
        queue_file=queue_file,
        force_rebuild=rebuild_queue,
    )

    if verbose:
        print("Queue stats before:", queue_stats(queue))

    tmdb = TMDBClient(
        api_key=api_key,
        bearer_token=bearer_token,
        cache_file=tmdb_cache_file,
        verbose=verbose,
    )

    image_base, profile_sizes = get_tmdb_image_base(tmdb)

    if image_size not in profile_sizes:
        if "w185" in profile_sizes:
            image_size = "w185"
        elif profile_sizes:
            image_size = profile_sizes[-1]

    processed_this_run = 0
    changed_since_save = 0

    actors = queue.get("actors", {})

    for actor_name, item in actors.items():
        status = item.get("status", "pending")

        if status == "ok" and not refresh_ok:
            continue

        if status == "failed" and not retry_failed:
            continue

        if status == "error" and not retry_error:
            continue

        if status not in ("pending", "ok", "failed", "error"):
            continue

        if limit is not None and processed_this_run >= limit:
            break

        if verbose:
            print(f"[{processed_this_run + 1}] {actor_name} [{status}]")

        item["tries"] = int(item.get("tries") or 0) + 1
        item["updated"] = now_iso()
        item["fail_reason"] = None

        try:
            result = resolve_actor_from_contexts(
                tmdb=tmdb,
                actor_name=actor_name,
                contexts=item.get("contexts") or [],
                image_base=image_base,
                image_size=image_size,
                max_images_per_actor=max_images_per_actor,
            )

            if not result.get("ok") and allow_name_fallback:
                result = fallback_person_search(
                    tmdb=tmdb,
                    name=actor_name,
                    contexts=item.get("contexts") or [],
                    image_base=image_base,
                    image_size=image_size,
                    max_images_per_actor=max_images_per_actor,
                )

            if result.get("ok"):
                item["status"] = "ok"
                item["urls"] = result.get("urls") or []
                item["tmdb_person_id"] = result.get("tmdb_person_id")
                item["tmdb_person_name"] = result.get("tmdb_person_name")
                item["matched_context"] = result.get("matched_context")
                item["movie_tmdb_id"] = result.get("movie_tmdb_id")
                item["method"] = result.get("movie_method") or result.get("method")
                item["failures"] = []
                item["fail_reason"] = None
            else:
                item["status"] = "failed"
                item["urls"] = []
                item["fail_reason"] = result.get("fail_reason") or result.get("reason")
                item["failures"] = result.get("failures") or [result]

            if include_old_nfo_urls:
                for ctx in item.get("contexts") or []:
                    old_thumb = ctx.get("old_nfo_thumb")

                    if valid_http_url(old_thumb):
                        item.setdefault("urls", [])

                        if old_thumb not in item["urls"]:
                            if max_images_per_actor is None:
                                item["urls"].append(old_thumb)
                            elif len(item["urls"]) < max_images_per_actor:
                                item["urls"].append(old_thumb)

                if item.get("urls") and item.get("status") == "failed":
                    item["status"] = "ok_old_nfo_url_only"

        except Exception as e:
            item["status"] = "error"
            item["fail_reason"] = repr(e)
            item["updated"] = now_iso()

        processed_this_run += 1
        changed_since_save += 1

        queue["_meta"]["updated"] = now_iso()
        queue["_meta"]["image_size"] = image_size
        queue["_meta"]["max_images_per_actor"] = max_images_per_actor

        if changed_since_save >= save_every:
            json_save_atomic(queue_file, queue)
            write_final_actor_db_from_queue(queue, cache_file)
            changed_since_save = 0

    json_save_atomic(queue_file, queue)
    actor_thumbs = write_final_actor_db_from_queue(queue, cache_file)

    if verbose:
        print("Queue stats after:", queue_stats(queue))
        print(f"Final actor image entries: {len(actor_thumbs)}")
        print(f"Processed this run: {processed_this_run}")

    return actor_thumbs


# ---------------------------------------------------------------------
# Example direct run
# ---------------------------------------------------------------------

if __name__ == "__main__":
    DIRS = [
        "/mnt/2tb/Movie",
    ]

    actor_thumbs = collect_actor_thumb_urls(
        DIRS,
        # Better: export TMDB_API_KEY='your_key' instead of hardcoding.
        api_key=None,
        bearer_token=None,

        # 'True' on first run or to flush:
        rebuild_queue=False,

        # Tiny test:
        #limit=50,

        # Conservative default:
        allow_name_fallback=False,

        # Usually enough:
        max_images_per_actor=1,

        verbose=True,
    )
