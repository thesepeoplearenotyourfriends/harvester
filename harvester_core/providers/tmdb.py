"""Narrow TMDB adapter with durable raw-response caching."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from ..storage import load_json, save_json_atomic


class TMDBClient:
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key=None, bearer_token=None, cache_file=None, transport=None):
        if not api_key and not bearer_token:
            raise ValueError(
                "TMDB capability unavailable: set TMDB_API_KEY or "
                "TMDB_BEARER_TOKEN"
            )
        self.api_key = api_key
        self.bearer_token = bearer_token
        self.cache_file = cache_file
        self.cache = load_json(cache_file, {}) if cache_file else {}
        self.transport = transport

    def _save(self):
        if self.cache_file:
            save_json_atomic(self.cache_file, self.cache)

    def get(self, path, params=None):
        params = {"language": "en-US", **(params or {})}
        cache_key = path + "?" + urllib.parse.urlencode(
            sorted(params.items()), doseq=True
        )
        if cache_key in self.cache:
            return self.cache[cache_key]

        request_params = dict(params)
        headers = {"Accept": "application/json", "User-Agent": "harvester/1"}
        if self.bearer_token:
            headers["Authorization"] = "Bearer " + self.bearer_token
        else:
            request_params["api_key"] = self.api_key
        url = self.BASE + path + "?" + urllib.parse.urlencode(request_params)

        for attempt in range(5):
            try:
                request = urllib.request.Request(url, headers=headers)
                opener = self.transport.open if self.transport else urllib.request.urlopen
                with opener(request, timeout=10) as response:
                    result = json.loads(response.read().decode("utf-8"))
                self.cache[cache_key] = result
                self._save()
                return result
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    self.cache[cache_key] = {}
                    self._save()
                    return {}
                if error.code != 429 or attempt == 4:
                    # Do not chain urllib's exception: it contains the API-key URL.
                    detail = ""
                    try:
                        payload = json.loads(error.read().decode("utf-8"))
                        status_code = payload.get("status_code")
                        status_message = payload.get("status_message")
                        if status_code is not None or status_message:
                            detail = ": " + " ".join(
                                str(value) for value in (status_code, status_message)
                                if value is not None and value != ""
                            )
                    except (ValueError, UnicodeError, AttributeError, OSError):
                        pass
                    raise RuntimeError(
                        f"TMDB request failed: HTTP {error.code}{detail}"
                    ) from None
                retry = error.headers.get("Retry-After")
                delay = float(retry) if retry and retry.replace(".", "", 1).isdigit() else 0.3
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                if attempt == 4:
                    raise RuntimeError(
                        f"TMDB request failed after retries: {type(error).__name__}"
                    ) from None
                time.sleep(attempt)
        raise RuntimeError("TMDB request failed after retries")
