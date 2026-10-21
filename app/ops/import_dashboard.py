"""Import the chart's HyperDX dashboards: create each by name, or update it in place.

Run by the Helm post-install/upgrade Job (``python -m app.ops.import_dashboard FILE``)
with ``HYPERDX_URL`` and ``HYPERDX_API_KEY``. The file is a HyperDX dashboard export (or
a list of them). Tiles name their source (e.g. ``"Job Events"``); the import resolves
those names to the ids the API expects, so the file stays portable between installs.
Waits for HyperDX to come up, since the Job can start before it's Ready.

``plan_import`` and ``resolve_sources`` are pure; ``main`` is the I/O around them.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Literal

import httpx

logger = logging.getLogger("import-dashboard")

# HyperDX's external API, authenticated with an API key. Overridable, since the paths
# have moved between HyperDX releases.
DASHBOARDS_PATH = os.environ.get("HYPERDX_DASHBOARDS_PATH", "/api/v2/dashboards")
SOURCES_PATH = os.environ.get("HYPERDX_SOURCES_PATH", "/api/v2/sources")
WAIT_SECONDS = 300.0

Action = tuple[Literal["create", "update"], dict[str, Any], str | None]


def load_dashboards(text: str) -> list[dict[str, Any]]:
    """The dashboards in an export file: one object, or a list of them."""
    data = json.loads(text)
    dashboards = data if isinstance(data, list) else [data]
    for d in dashboards:
        if not isinstance(d, dict) or not d.get("name"):
            raise ValueError("every dashboard needs a name")
    return dashboards


def resolve_sources(dashboard: dict[str, Any], source_ids: dict[str, str]) -> dict[str, Any]:
    """A copy of ``dashboard`` with each tile's source name replaced by its id. Raises
    ``ValueError`` naming every source the HyperDX instance doesn't have."""
    known = set(source_ids) | set(source_ids.values())  # a name, or already an id
    sources = {tile.get("config", {}).get("source") for tile in dashboard.get("tiles", [])}
    missing = sorted(s for s in sources if s is not None and s not in known)
    if missing:
        raise ValueError(f"dashboard {dashboard['name']!r} uses unknown sources: {missing}")
    resolved: dict[str, Any] = json.loads(json.dumps(dashboard))
    for tile in resolved.get("tiles", []):
        source = tile.get("config", {}).get("source")
        if source in source_ids:
            tile["config"]["source"] = source_ids[source]
    return resolved


def plan_import(wanted: list[dict[str, Any]], existing: list[dict[str, Any]]) -> list[Action]:
    """For each wanted dashboard: update the existing one with that name (by its id), or
    create it. Dashboards the file doesn't mention are left alone."""
    ids = {d.get("name"): d.get("id") or d.get("_id") for d in existing}
    actions: list[Action] = []
    for dashboard in wanted:
        existing_id = ids.get(dashboard["name"])
        if existing_id:
            actions.append(("update", dashboard, str(existing_id)))
        else:
            actions.append(("create", dashboard, None))
    return actions


def _items(body: Any) -> list[dict[str, Any]]:
    # The API wraps lists as {"data": [...]}; accept a bare list too.
    items = body.get("data", []) if isinstance(body, dict) else body
    return [i for i in items if isinstance(i, dict)]


def _wait_until_up(http: httpx.Client) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        try:
            if http.get(DASHBOARDS_PATH).status_code < 500:
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError(f"HyperDX not reachable after {WAIT_SECONDS:.0f}s")
        logger.info("waiting for HyperDX...")
        time.sleep(5)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO)
    if len(argv) != 2:
        print("usage: python -m app.ops.import_dashboard DASHBOARD.json", file=sys.stderr)
        return 2
    wanted = load_dashboards(Path(argv[1]).read_text())
    headers = {"Authorization": f"Bearer {os.environ['HYPERDX_API_KEY']}"}
    with httpx.Client(base_url=os.environ["HYPERDX_URL"], headers=headers, timeout=30) as http:
        _wait_until_up(http)
        sources = _items(http.get(SOURCES_PATH).raise_for_status().json())
        source_ids = {s["name"]: str(s.get("id") or s.get("_id")) for s in sources}
        existing = _items(http.get(DASHBOARDS_PATH).raise_for_status().json())
        for action, dashboard, dashboard_id in plan_import(wanted, existing):
            body = resolve_sources(dashboard, source_ids)
            if action == "update":
                http.put(f"{DASHBOARDS_PATH}/{dashboard_id}", json=body).raise_for_status()
            else:
                http.post(DASHBOARDS_PATH, json=body).raise_for_status()
            logger.info("%sd dashboard %r", action, dashboard["name"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
