#!/usr/bin/env python3
"""Submit a job and verify the WS event arrives."""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import websockets

TAAS_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
API_KEY = sys.argv[2] if len(sys.argv) > 2 else None
IMAGE = sys.argv[3] if len(sys.argv) > 3 else None

if not API_KEY or not IMAGE:
    print(f"Usage: {sys.argv[0]} <taas_url> <api_key> <image_path>")
    sys.exit(1)


async def main():
    ws_url = TAAS_URL.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/ws?api_key={API_KEY}"

    ext_id = str(uuid4())

    # Connect WS first
    async with websockets.connect(ws_url) as ws:
        print(f"WS connected. Submitting job with uuid={ext_id}...")

        # Submit job via HTTP
        async with httpx.AsyncClient() as http:
            with open(IMAGE, "rb") as f:
                resp = await http.post(
                    f"{TAAS_URL}/api/v1/jobs",
                    headers={"X-API-Key": API_KEY},
                    files={"image": (Path(IMAGE).name, f)},
                    data={"uuid": ext_id, "fmt": os.environ.get("FMT", "multi")},
                )
                resp.raise_for_status()
                job = resp.json()
                print(f"Submitted: job_id={job['job_id']}")

        # Wait for WS event
        print("Waiting for WS event...")
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=120)
            event = json.loads(raw)
            print(f"Event: {json.dumps(event, indent=2)}")
            if event.get("uuid") == ext_id:
                if event["status"] == "done":
                    print(f"\nSUCCESS: got result for {ext_id}")
                    print(f"  alto_url: {event.get('alto_url', 'N/A')}")
                    print(f"  txt_url: {event.get('txt_url', 'N/A')}")
                elif event["status"] == "failed":
                    print(f"\nFAILED: {event.get('error')}")
                break


asyncio.run(main())
