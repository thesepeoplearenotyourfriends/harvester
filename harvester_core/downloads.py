"""Small shared HTTP image downloader; provider jobs supply their identity."""
import time
import urllib.error
import urllib.request

from .events import emit


def download_image(url, *, user_agent, request_attempts=4, request_timeout=30,
                   reporter=None, sleep=None, transport=None):
    """Download one image with bounded retries for transient failures."""
    sleep = sleep or time.sleep
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError(f"not an HTTP image URL: {url!r}")
    request = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "image/jpeg,image/png,image/webp,image/*,*/*",
    })
    last_error = None
    for attempt in range(request_attempts):
        try:
            opener = transport.open if transport else urllib.request.urlopen
            with opener(request, timeout=request_timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                data = response.read()
            if not data:
                raise RuntimeError("downloaded zero bytes")
            return data, content_type
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise RuntimeError("HTTP 404") from None
            last_error = error
            if error.code != 429 and not 500 <= error.code <= 599:
                break
            if attempt == request_attempts - 1:
                break
            retry_after = error.headers.get("Retry-After")
            delay = (float(retry_after) if retry_after and
                     retry_after.replace(".", "", 1).isdigit()
                     else min(2 ** attempt, 20))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt == request_attempts - 1:
                break
            delay = min(2 ** attempt, 20)
        emit(reporter, "retry", "image request retry",
             attempt=attempt + 1, delay=delay)
        sleep(delay)
    error_name = type(last_error).__name__ if last_error else "unknown error"
    raise RuntimeError(f"image request failed after retries: {error_name}")
