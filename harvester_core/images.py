"""Image helpers whose Pillow enhancement remains strictly optional."""
from io import BytesIO
import re

MAX_UI_IMAGE_BYTES = 50 * 1024

def safe_actor_filename(name):
    name = re.sub(r"[\\/:]+", "_", (name or "").strip())
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^A-Za-z0-9._'()&+\-]+", "_", name).strip("._ ")
    return (name or "unknown_actor") + ".jpg"


def normalize_actor_image(data, enabled=True, max_size=(185, 278), quality=75):
    if not enabled:
        return data
    try:
        from PIL import Image
    except Exception:
        # Broken native Pillow installations are as optional as absent ones.
        return data
    try:
        with Image.open(BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail(max_size, Image.LANCZOS)
            output = BytesIO()
            image.save(output, "JPEG", quality=quality, optimize=True, progressive=False)
            return output.getvalue()
    except Exception:
        return data


def normalize_ui_image(data, max_bytes=MAX_UI_IMAGE_BYTES, max_size=(600, 900)):
    """Validate and make a bounded JPEG for manually supplied UI artwork.

    This capability deliberately requires Pillow: returning the original bytes
    would falsely claim that an arbitrary remote payload had been converted and
    bounded.  Pillow remains a lazy optional enhancement for the rest of core.
    """
    try:
        from PIL import Image
    except Exception as error:
        raise RuntimeError("image URL import requires the optional Pillow package") from error
    try:
        with Image.open(BytesIO(data)) as opened:
            opened.verify()
        with Image.open(BytesIO(data)) as opened:
            image = opened.convert("RGB")
            image.thumbnail(max_size, Image.LANCZOS)
            for quality in (82, 72, 62, 52, 42, 32, 24):
                output = BytesIO()
                image.save(output, "JPEG", quality=quality, optimize=True, progressive=False)
                if output.tell() <= max_bytes:
                    return output.getvalue()
            while image.width > 160 and image.height > 160:
                image.thumbnail((max(160, image.width * 3 // 4),
                                 max(160, image.height * 3 // 4)), Image.LANCZOS)
                output = BytesIO()
                image.save(output, "JPEG", quality=24, optimize=True, progressive=False)
                if output.tell() <= max_bytes:
                    return output.getvalue()
    except Exception as error:
        raise ValueError("URL did not provide a readable image") from error
    raise ValueError("image could not be reduced below 50 KB")
