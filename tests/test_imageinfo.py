"""Tests for reading an image's megapixels from its header (step 8, D2)."""

from io import BytesIO

from PIL import Image

from app.services.imageinfo import UNKNOWN_MEGAPIXELS, image_megapixels


def test_megapixels_from_a_png() -> None:
    buf = BytesIO()
    Image.new("L", (2, 3)).save(buf, format="PNG")
    assert image_megapixels(buf.getvalue()) == 6e-6


def test_garbage_counts_as_one_megapixel() -> None:
    assert image_megapixels(b"not an image") == UNKNOWN_MEGAPIXELS
    assert image_megapixels(b"") == UNKNOWN_MEGAPIXELS
