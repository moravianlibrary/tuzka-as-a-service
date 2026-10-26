"""Image facts read at submit, cheaply (step 8, D2)."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, UnidentifiedImageError

# What a page counts as when its size can't be read: a typical scan is ~10 MP, but a
# neutral 1.0 keeps a broken header from skewing the speed scores either way.
UNKNOWN_MEGAPIXELS = 1.0


def image_megapixels(data: bytes) -> float:
    """Width × height / 1e6, from the image header only (Pillow doesn't decode the
    pixels until asked), so it costs microseconds even for a large TIFF."""
    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError):
        return UNKNOWN_MEGAPIXELS
    if width <= 0 or height <= 0:
        return UNKNOWN_MEGAPIXELS
    return width * height / 1_000_000
