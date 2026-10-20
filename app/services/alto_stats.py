"""Counts read from a job's ALTO for its done event (blocks, characters)."""

import logging
from xml.etree import ElementTree

logger = logging.getLogger(__name__)


def parse_alto(alto_bytes: bytes) -> tuple[int, int]:
    """Return (blocks, chars) for an ALTO XML document.

    Blocks = TextBlock count, chars = sum of String CONTENT lengths. The line count is
    taken from the engine's ``n_lines`` (not parsed here). Returns (0, 0) on parse failure."""
    try:
        root = ElementTree.fromstring(alto_bytes)
        tag = root.tag
        if "}" in tag:
            ns_uri = tag.split("}")[0].lstrip("{")
            ns = {"a": ns_uri}
            blocks = len(root.findall(".//a:TextBlock", ns))
            chars = sum(len(e.get("CONTENT", "")) for e in root.findall(".//a:String", ns))
        else:
            blocks = len(root.findall(".//TextBlock"))
            chars = sum(len(e.get("CONTENT", "")) for e in root.findall(".//String"))
        return blocks, chars
    except Exception as exc:
        logger.warning("ALTO parse failed: %s", exc)
        return 0, 0
