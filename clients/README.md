# `clients/` — taas client libraries

Thin, event-driven clients for the taas API. Both follow the same model:

- you **submit** images (each gets a UUID),
- the client holds a **WebSocket** to `/ws` and dispatches your callbacks as results arrive,
- results are **fetched from the presigned URL and zstd-decompressed** for you, so callbacks
  receive ready-to-use ALTO/text bytes.

| Language | Path | Package |
|---|---|---|
| Python | [`python/`](python/) | `taas-client` (PyPI-style), module `taas_client` |
| Java | [`java/`](java/) | `cz.osdd.taas:taas-client:0.1.0` |

You need a user API key (created via `POST /admin/users`, see the [app README](../app/README.md)).

## Python

```bash
pip install ./clients/python      # or: pip install taas-client
```

```python
from taas_client import TaasClient, JobResult, JobEvent

def on_result(r: JobResult):
    print(r.uuid, "alto:", len(r.alto or b""), "txt:", len(r.txt or b""))

def on_error(e: JobEvent):
    print(r.uuid, "FAILED:", e.error)

client = TaasClient(
    url="http://localhost:8080",
    api_key="<user-api-key>",
    on_result=on_result,
    on_error=on_error,
    fmt="multi",                  # "alto" | "txt" | "multi"
)
client.start()                    # opens the WebSocket

# provide your own UUID to correlate results with your system...
from uuid import UUID
client.submit("page-1.jpg", uuid=UUID("0109e8c2-d262-11e1-b9d7-0050569d679d"))
# ...or omit it and the client generates one, returning it:
job_id = client.submit("page-2.tif")

client.wait()                     # block until all submitted jobs resolve
client.stop()
```

`JobResult(uuid, alto, txt)` carries decompressed bytes; `JobEvent` carries
`status`, `error`, `alto_url`/`txt_url`, `ts`. An `AsyncTaasClient` is also exported.
Deps: `httpx`, `websockets`, `zstandard`.

## Java

```bash
cd clients/java && mvn install
```

```java
import cz.osdd.taas.client.TaasClient;
import java.nio.file.Path;

try (TaasClient client = new TaasClient(
        "http://localhost:8080", "<user-api-key>",
        result -> System.out.println(result.uuid() + " alto=" +
            (result.alto() == null ? 0 : result.alto().length)),
        event  -> System.err.println(event.uuid() + " FAILED: " + event.error()))) {

    client.start();                       // opens the WebSocket
    UUID id = client.submit(Path.of("page-1.jpg"));
    client.awaitAll();                    // block until all jobs resolve
}
```

The 5-arg constructor adds default `fmt` and `domain`. `submit` is overloaded: pass your own
UUID — `submit(path, uuid)` or `submit(path, uuid, filename, fmt, domain)` — or omit it
(`submit(path)` / `submit(path, filename, fmt, domain)`) and it generates one. All overloads
return the job UUID. Deps: OkHttp + Gson (see `pom.xml`).

## Notes

- **Job UUID (`external_id`)** — the taas API requires a UUID per job; the client always sends
  one, so it's **optional in `submit`**: pass your own to correlate results with your system
  (must be a valid UUID, unique per user), or omit it and the client generates one. It's echoed
  back on every event and `JobResult`/`JobEvent`.
- `fmt="multi"` returns both ALTO and text and depends on engine-side multi support; until
  then use `alto` or `txt`.
- Both clients auto-reconnect the WebSocket and de-duplicate by UUID, so a brief drop won't
  lose results that arrived during the gap (replayed via the API's catch-up window).
