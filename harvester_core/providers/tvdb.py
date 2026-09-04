"""TVDB v4 adapter with token refresh and durable GET caching."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from ..storage import load_json, save_json_atomic


class TVDBClient:
    BASE = "https://api4.thetvdb.com/v4"
    USER_AGENT = "local-tv-tvdb-url-scanner/1.0"

    def __init__(self, api_key=None, pin=None, cache_file=None, transport=None,
                 request_timeout=20, request_attempts=5):
        if not api_key:
            raise ValueError("TVDB capability unavailable: set TVDB_API_KEY")
        self.api_key = api_key
        self.pin = pin
        self.token = None
        self.cache_file = cache_file
        self.cache = load_json(cache_file, {}) if cache_file else {}
        self.transport = transport
        self.request_timeout = request_timeout
        self.request_attempts = request_attempts

    def _request(self, method, path, params=None, payload=None, authenticated=True):
        headers = {"Accept": "application/json", "User-Agent": self.USER_AGENT}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        if authenticated:
            if not self.token:
                self.login()
            headers["Authorization"] = "Bearer " + self.token
        url = self.BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(sorted(params.items()), doseq=True)
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        opener = self.transport.open if self.transport else urllib.request.urlopen
        with opener(request, timeout=self.request_timeout) as response:
            body = response.read().decode("utf-8")
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"TVDB returned non-JSON data for {method} {path}: {error!r}"
            ) from error

    def login(self):
        payload = {"apikey": self.api_key}
        if self.pin:
            payload["pin"] = self.pin
        try:
            response = self._request("POST", "/login", payload=payload, authenticated=False)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"TVDB login failed: HTTP {error.code}") from None
        self.token = ((response.get("data") or {}).get("token") or "").strip()
        if not self.token:
            raise RuntimeError("TVDB login returned no bearer token")

    def get(self, path, params=None):
        params = dict(params or {})
        key = path + "?" + urllib.parse.urlencode(sorted(params.items()), doseq=True)
        if key in self.cache:
            return self.cache[key], True
        refreshed = False
        for attempt in range(self.request_attempts):
            try:
                response = self._request("GET", path, params=params)
                data = response.get("data")
                self.cache[key] = data
                if self.cache_file:
                    save_json_atomic(self.cache_file, self.cache)
                return data, False
            except urllib.error.HTTPError as error:
                if error.code == 401 and not refreshed:
                    self.token = None
                    refreshed = True
                    self.login()
                    continue
                if error.code == 404:
                    self.cache[key] = {}
                    if self.cache_file:
                        save_json_atomic(self.cache_file, self.cache)
                    return {}, False
                if error.code != 429 and not 500 <= error.code < 600:
                    raise RuntimeError(f"TVDB request failed: HTTP {error.code}") from None
                retry = error.headers.get("Retry-After")
                delay = float(retry) if retry and retry.replace(".", "", 1).isdigit() else min(2 ** attempt, 15)
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                if attempt == self.request_attempts - 1:
                    raise RuntimeError(
                        f"TVDB request failed after retries: {type(error).__name__}"
                    ) from None
                delay = min(2 ** attempt, 15)
            if attempt == self.request_attempts - 1:
                raise RuntimeError("TVDB request failed after retries") from None
            time.sleep(delay)
        raise RuntimeError("TVDB request failed after retries")
