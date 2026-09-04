"""Image helpers whose Pillow enhancement remains strictly optional."""
from io import BytesIO
import re

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
