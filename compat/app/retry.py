import asyncio
import random

import httpx
from fastapi import HTTPException

# Legacy PERO clients are synchronous and cannot handle 429: absorb taas rate
# limiting here by waiting for the GCRA window, bounded so we stay under the
# client's own HTTP timeout.
RETRY_BUDGET_SECONDS = 30.0
MAX_SLEEP_PER_ATTEMPT = 10.0
JITTER_MAX_SECONDS = 0.5


async def request_with_retry(
    http: httpx.AsyncClient, method: str, url: str, **kwargs
) -> httpx.Response:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RETRY_BUDGET_SECONDS
    while True:
        resp = await http.request(method, url, **kwargs)
        if resp.status_code != 429:
            return resp
        try:
            retry_after = float(resp.headers.get("Retry-After", "1"))
        except ValueError:
            retry_after = 1.0
        sleep = min(retry_after, MAX_SLEEP_PER_ATTEMPT) + random.uniform(
            0, JITTER_MAX_SECONDS
        )
        if loop.time() + sleep > deadline:
            raise HTTPException(status_code=503, detail="Service busy, retry later")
        await asyncio.sleep(sleep)
