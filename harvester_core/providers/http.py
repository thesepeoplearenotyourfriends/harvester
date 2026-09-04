import json, time, urllib.error, urllib.parse, urllib.request

def request_json(url, headers=None, payload=None, attempts=5, timeout=20):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    last = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            last = error
            if error.code not in (429, 500, 502, 503, 504): raise
            retry = error.headers.get("Retry-After")
            delay = float(retry) if retry and retry.replace(".", "", 1).isdigit() else min(2 ** attempt, 15)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last = error; delay = min(2 ** attempt, 15)
        if attempt + 1 < attempts: time.sleep(delay)
    raise RuntimeError(f"provider request failed after {attempts} attempts: {last!r}")
