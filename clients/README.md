# `clients/` — taas client libraries

Thin, event-driven clients for the taas API. Both do two modes, and you can mix them in
one client:

- **Per page** — you **submit** images one by one (each gets a job id, and your own label if you give one); the client holds a
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
| Java | [`java/`](java/) | `cz.osdd.taas:taas-client:0.2.0` |

You need a user API key (created with `taas-admin user create <name>`, see the [app README](../app/README.md#administration--taas-admin)).

## Python

```bash
pip install ./clients/python      # or: pip install taas-client
```

```python
from taas_client import TaasClient, JobResult, JobEvent

def on_result(r: JobResult):
    print(r.external_id, "alto:", len(r.alto or b""), "txt:", len(r.txt or b""))

def on_error(e: JobEvent):
    print(e.external_id, "FAILED:", e.error)

client = TaasClient(
    url="http://localhost:8080",
    api_key="<user-api-key>",
    on_result=on_result,
    on_error=on_error,
    fmt="multi",                  # "alto" | "txt" | "multi"
)
client.start()                    # opens the WebSocket

# label the page with your own id (any string), echoed back on its result...
job_id = client.submit("page-1.jpg", external_id="uuid:0109e8c2-d262-11e1-b9d7-0050569d679d")
# ...or don't, and use the returned job id:
job_id = client.submit("page-2.tif")

client.wait()                     # block until all submitted jobs resolve
client.stop()
```

`JobResult(job_id, external_id, alto, txt)` carries decompressed bytes; `JobEvent`
carries `job_id`, `external_id`, `status`, `error`, `alto_url`/`txt_url`, `ts`. An `AsyncTaasClient` is also exported,
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

batch = client.submit_batch("pages/", external_id="monograph-123",  # a directory, a list
                            domain="kramarky")                     # of images, or an archive
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
        result -> System.out.println(result.externalId() + " alto=" +
            (result.alto() == null ? 0 : result.alto().length)),
        event  -> System.err.println(event.externalId() + " FAILED: " + event.error()))) {

    client.start();                       // opens the WebSocket
    UUID jobId = client.submit(Path.of("page-1.jpg"), "uuid:0109e8c2-…");  // your label
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

`submit` is overloaded: with or without your own label (`submit(path, externalId,
options)`), with or without `SubmitOptions`. All overloads return the job id.

Batch mode:

```java
client.onBatchProgress(p -> System.out.println(p.done() + p.failed() + "/" + p.total()));
BatchSubmit batch = client.submitBatch(Path.of("pages/"), "monograph-123", kramarky);
BatchStatus status = client.awaitBatch(batch.batchId(), 1, TimeUnit.HOURS);
BatchResults results = client.downloadBatchResults(batch.batchId(), Path.of("out"), true);
```

```bash
cd clients/java && mvn -q compile exec:java -Dexec.args="http://localhost:8080 KEY ../../test-data"
```

Deps: OkHttp, Gson, zstd-jni, commons-compress (see `pom.xml`).

## Notes

- **Identifiers** — every job has a `job_id`, chosen by taas and returned by `submit`.
  `external_id` is **your** optional label: any string up to 255 characters (a PID like
  `uuid:…`, a signature, a file name), not required to be unique. On a page it names the
  page and comes back on its result; on a batch it names the batch. Batch pages have no
  `external_id` of their own: the manifest identifies each by `source_name`.
- `fmt="multi"` returns both ALTO and text and depends on engine-side multi support; until
  then use `alto` or `txt`.
- **Batch pages** — a batch's pages run as ordinary jobs, but the clients don't download
  them one by one: the results come as the batch's archive. Map them back with the
  manifest's `source_name` (the path inside the archive you sent).
- Both clients auto-reconnect the WebSocket and de-duplicate by job id, so a brief drop won't
  lose results that arrived during the gap (replayed via the API's catch-up window).
