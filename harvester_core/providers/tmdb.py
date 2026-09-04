"""Narrow TMDB adapter with durable raw-response caching."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from ..storage import load_json, save_json_atomic


class TMDBClient:
    BASE = "https://api.themoviedb.org/3"
    USER_AGENT = "harvester/1"

    def __init__(self, api_key=None, bearer_token=None, cache_file=None, transport=None,
                 language="en-US", request_timeout=10, request_attempts=5):
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
        self.language = language
        self.request_timeout = request_timeout
        self.request_attempts = request_attempts

    def _save(self):
        if self.cache_file:
            save_json_atomic(self.cache_file, self.cache)

    def get(self, path, params=None):
        params = dict(params or {})
        params.setdefault("language", self.language)
        cache_params = {key: value for key, value in params.items() if key != "api_key"}
        cache_key = path + "?" + urllib.parse.urlencode(
            sorted(cache_params.items()), doseq=True
        )
        if cache_key in self.cache:
            return self.cache[cache_key]

        request_params = dict(params)
        headers = {"Accept": "application/json", "User-Agent": self.USER_AGENT}
        if self.bearer_token:
            headers["Authorization"] = "Bearer " + self.bearer_token
        else:
            request_params["api_key"] = self.api_key
        url = self.BASE + path + "?" + urllib.parse.urlencode(request_params)

        for attempt in range(self.request_attempts):
            try:
                request = urllib.request.Request(url, headers=headers)
                opener = self.transport.open if self.transport else urllib.request.urlopen
                with opener(request, timeout=self.request_timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
                self.cache[cache_key] = result
                self._save()
                return result
            except urllib.error.HTTPError as error:
                if error.code == 429:
                    retry = error.headers.get("Retry-After")
                    delay = int(retry) if retry and retry.isdigit() else 0.3
                    time.sleep(delay)
                    continue
                if error.code == 404:
                    self.cache[cache_key] = {}
                    self._save()
                    return {}
                if error.code != 429 or attempt == self.request_attempts - 1:
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
            except (urllib.error.URLError, TimeoutError, OSError,
                    json.JSONDecodeError, UnicodeError) as error:
                if attempt == self.request_attempts - 1:
                    raise RuntimeError(
                        f"TMDB request failed after retries: {type(error).__name__}"
                    ) from None
                time.sleep(attempt)
        # The reference adapter treats exhausted 429 responses as an empty
        # response rather than turning throttling into an actor-level error.
        return {}
