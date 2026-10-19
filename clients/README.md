# `clients/` — taas client libraries

Thin, event-driven clients for the taas API. Both do two modes, and you can mix them in
one client:

- **Per page** — you **submit** images one by one (each gets a UUID); the client holds a
  **WebSocket** to `/ws` and calls your callbacks as each page finishes, with results
  **fetched from the presigned URL and zstd-decompressed** for you. Best for interactive
  use and small sets.
- **Batch** — you submit a whole directory (or an archive) **once**; the client packs it
  into a `.tar.zst`, reports progress as the pages finish, and downloads **one archive**
  of all outputs with a `manifest.json`. Best for large sets.

Every submit names a `domain`, the `outputs` it wants (e.g. `alto,txt`; the older `fmt`
still works) and engine `options`. `domains()` lists what the server offers: each
domain's outputs and the JSON Schema its options are validated against.

Runnable demos run the same images both ways and time them:
[`python/examples/demo.py`](python/examples/demo.py) and
[`Demo.java`](java/src/main/java/cz/osdd/taas/client/examples/Demo.java).

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
`status`, `error`, `alto_url`/`txt_url`, `ts`. An `AsyncTaasClient` is also exported,
with the same methods as coroutines. Deps: `httpx`, `websockets`, `zstandard`.

Domain, outputs and options — as client defaults, or per submit:

```python
for d in client.domains():
    print(d.name, d.outputs, d.options_schema)

client.submit("page-3.jpg", domain="kramarky", outputs=["alto"],
              options={"alto_profile": "basic"})
```

Batch mode:

```python
client = TaasClient(..., on_batch=lambda p: print(f"{p.done + p.failed}/{p.total}"))
client.start()

batch = client.submit_batch("pages/", domain="kramarky")   # a directory, a list of
                                                            # images, or an archive
status = client.wait_batch(batch.batch_id)                  # done / failed counts
results = client.download_batch_results(batch.batch_id, "out/", extract=True)
for page in results.manifest:                               # out/<batch_id>/manifest.json
    print(page["source_name"], page["status"], page.get("outputs"))
```

`get_batch(batch_id, pages=True)` lists every page's status while it runs.

```bash
python clients/python/examples/demo.py --api-key KEY test-data/ [--domain kramarky]
```

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

`SubmitOptions` carries the domain, outputs and options — as client defaults (last
constructor argument) or per submit:

```java
SubmitOptions kramarky = SubmitOptions.defaults()
    .withDomain("kramarky").withOutputs("alto", "txt")
    .withOptions(Map.of("alto_profile", "basic"));
client.submit(Path.of("page-2.jpg"), kramarky);
client.domains().forEach(d -> System.out.println(d.name() + " " + d.outputs()));
```

`submit` is overloaded: pass your own UUID (`submit(path, uuid, options)`) or omit it and
it generates one; the older `fmt`/`domain` overloads still work. All overloads return the
job UUID.

Batch mode:

```java
client.onBatchProgress(p -> System.out.println(p.done() + p.failed() + "/" + p.total()));
BatchSubmit batch = client.submitBatch(Path.of("pages/"), kramarky);  // directory or archive
BatchStatus status = client.awaitBatch(batch.batchId(), 1, TimeUnit.HOURS);
BatchResults results = client.downloadBatchResults(batch.batchId(), Path.of("out"), true);
```

```bash
cd clients/java && mvn -q compile exec:java -Dexec.args="http://localhost:8080 KEY ../../test-data"
```

Deps: OkHttp, Gson, zstd-jni, commons-compress (see `pom.xml`).

## Notes

- **Job UUID (`external_id`)** — the taas API requires a UUID per job; the client always sends
  one, so it's **optional in `submit`**: pass your own to correlate results with your system
  (must be a valid UUID, unique per user), or omit it and the client generates one. It's echoed
  back on every event and `JobResult`/`JobEvent`.
- `fmt="multi"` returns both ALTO and text and depends on engine-side multi support; until
  then use `alto` or `txt`.
- **Batch pages** — a batch's pages run as ordinary jobs, but the clients don't download
  them one by one: the results come as the batch's archive. Map them back with the
  manifest's `source_name` (the path inside the archive you sent).
- Both clients auto-reconnect the WebSocket and de-duplicate by UUID, so a brief drop won't
  lose results that arrived during the gap (replayed via the API's catch-up window).
