package cz.osdd.taas.client;

import com.github.luben.zstd.Zstd;
import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.google.gson.reflect.TypeToken;
import okhttp3.*;

import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.lang.reflect.Type;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/**
 * Thread-safe taas client for both modes:
 * <ul>
 *   <li><b>per page</b> — {@link #submit(Path, SubmitOptions)}; results arrive over the
 *       WebSocket and go to the {@link ResultHandler} / {@link ErrorHandler};</li>
 *   <li><b>batches</b> — {@link #submitBatch(Path, SubmitOptions)}; one archive in, one
 *       archive of results out ({@link #downloadBatchResults}), progress to an optional
 *       {@link BatchHandler}.</li>
 * </ul>
 * Designed for a single start()/stop() lifecycle.
 */
public class TaasClient implements AutoCloseable {

    private static final Type SCHEMA_TYPE = new TypeToken<Map<String, Object>>() {}.getType();

    private final String baseUrl;
    private final String apiKey;
    private final ResultHandler onResult;
    private final ErrorHandler onError;
    private final SubmitOptions defaults;
    private volatile BatchHandler onBatch;

    private final OkHttpClient http;
    private final Gson gson;

    // Job ids awaited by awaitAll, and ids whose result arrived before their submit
    // returned (insertion-ordered, capped); both guarded by `pendingLock`.
    private final Object pendingLock = new Object();
    private final Set<UUID> pending = new HashSet<>();
    private final Map<UUID, Boolean> finishedEarly = new LinkedHashMap<>();
    private static final int FINISHED_EARLY_MAX = 10_000;
    private final CountDownLatch doneLatch = new CountDownLatch(1);
    // Batches someone waits on (awaitBatch), released by a finished progress event.
    private final Map<UUID, CountDownLatch> batchWaiters = new ConcurrentHashMap<>();
    private volatile boolean started = false;
    private volatile WebSocket ws;

    public TaasClient(
        String url,
        String apiKey,
        ResultHandler onResult,
        ErrorHandler onError
    ) {
        this(url, apiKey, onResult, onError, SubmitOptions.defaults());
    }

    public TaasClient(
        String url,
        String apiKey,
        ResultHandler onResult,
        ErrorHandler onError,
        String fmt,
        String domain
    ) {
        this(url, apiKey, onResult, onError, SubmitOptions.defaults().withFmt(fmt).withDomain(domain));
    }

    /** {@code defaults} apply to every submit that doesn't pass its own options. */
    public TaasClient(
        String url,
        String apiKey,
        ResultHandler onResult,
        ErrorHandler onError,
        SubmitOptions defaults
    ) {
        this.baseUrl = url.endsWith("/") ? url.substring(0, url.length() - 1) : url;
        this.apiKey = apiKey;
        this.onResult = onResult;
        this.onError = onError;
        this.defaults = defaults;
        this.http = new OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .build();
        this.gson = new Gson();
    }

    /** Receive batch progress events (on the WebSocket thread). Returns this client. */
    public TaasClient onBatchProgress(BatchHandler handler) {
        this.onBatch = handler;
        return this;
    }

    public void start() {
        if (started) throw new IllegalStateException("Already started");
        started = true;
        connectWebSocket();
    }

    public void stop() {
        started = false;
        if (ws != null) {
            ws.close(1000, "client shutdown");
        }
        http.dispatcher().executorService().shutdown();
    }

    @Override
    public void close() {
        stop();
    }

    // ---- discovery ----

    /** The domains you can submit to, with their outputs and options schema. */
    public List<DomainInfo> domains() {
        JsonObject body = getJson(baseUrl + "/api/v1/domains");
        List<DomainInfo> domains = new ArrayList<>();
        for (JsonElement el : body.getAsJsonArray("domains")) {
            JsonObject d = el.getAsJsonObject();
            Map<String, Object> schema = d.has("options_schema") && !d.get("options_schema").isJsonNull()
                ? gson.fromJson(d.get("options_schema"), SCHEMA_TYPE) : null;
            domains.add(new DomainInfo(
                d.get("name").getAsString(),
                str(d, "label"),
                str(d, "description"),
                strings(d.getAsJsonArray("outputs")),
                schema,
                d.has("legacy_engine_id") && !d.get("legacy_engine_id").isJsonNull()
                    ? d.get("legacy_engine_id").getAsInt() : null
            ));
        }
        return domains;
    }

    // ---- per-page mode ----

    public UUID submit(Path imagePath) {
        return submit(imagePath, null, defaults);
    }

    public UUID submit(Path imagePath, SubmitOptions options) {
        return submit(imagePath, null, options);
    }

    /** Submit with your own label for the page (e.g. its PID); it comes back on its result. */
    public UUID submit(Path imagePath, String externalId) {
        return submit(imagePath, externalId, defaults);
    }

    public UUID submit(Path imagePath, String externalId, SubmitOptions options) {
        return submit(imagePath, externalId, imagePath.getFileName().toString(), options);
    }

    /**
     * Submit one page and return its job id. {@code externalId} is your own label for the
     * page (any string up to 255 characters, or null); it comes back on the page's
     * {@link JobResult} / {@link JobEvent}.
     */
    public UUID submit(Path imagePath, String externalId, String filename, SubmitOptions options) {
        String mediaType = guessMediaType(filename);
        byte[] imageBytes;
        try {
            imageBytes = Files.readAllBytes(imagePath);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }

        MultipartBody.Builder bodyBuilder = new MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("image", filename,
                RequestBody.create(imageBytes, MediaType.parse(mediaType)));
        if (externalId != null) {
            bodyBuilder.addFormDataPart("external_id", externalId);
        }
        // outputs replaces fmt; send fmt only when no outputs were asked for
        if (options.outputs() != null) {
            bodyBuilder.addFormDataPart("outputs", String.join(",", options.outputs()));
        } else {
            bodyBuilder.addFormDataPart("fmt", options.fmt());
        }
        addDomainAndOptions(bodyBuilder, options);

        Request request = new Request.Builder()
            .url(baseUrl + "/api/v1/jobs")
            .header("X-API-Key", apiKey)
            .post(bodyBuilder.build())
            .build();

        UUID jobId;
        try (Response response = http.newCall(request).execute()) {
            if (!response.isSuccessful()) {
                throw new IOException("Submit failed: " + response.code()
                    + " " + response.body().string());
            }
            jobId = UUID.fromString(JsonParser.parseString(response.body().string())
                .getAsJsonObject().get("job_id").getAsString());
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }

        synchronized (pendingLock) {
            // A fast page can finish before its submit response arrives.
            if (finishedEarly.remove(jobId) == null) {
                pending.add(jobId);
            }
        }
        return jobId;
    }

    public boolean awaitAll() throws InterruptedException {
        return awaitAll(Long.MAX_VALUE, TimeUnit.MILLISECONDS);
    }

    public boolean awaitAll(long timeout, TimeUnit unit) throws InterruptedException {
        synchronized (pendingLock) {
            if (pending.isEmpty()) return true;
        }
        return doneLatch.await(timeout, unit);
    }

    // ---- batch mode ----

    public BatchSubmit submitBatch(Path source) {
        return submitBatch(source, null, defaults);
    }

    /**
     * Submit many pages as one batch. {@code source} is an archive ({@code .tar.zst},
     * {@code .tar.gz}, {@code .tar}, {@code .zip}), sent as-is, or a directory, packed
     * into a temporary {@code .tar.zst} first. {@code externalId} is your own label for
     * the batch (or null); its pages are identified by their path in the archive.
     */
    public BatchSubmit submitBatch(Path source, String externalId, SubmitOptions options) {
        try {
            if (Files.isRegularFile(source)) {
                if (!TarZst.isArchive(source)) {
                    throw new IllegalArgumentException(
                        source + " is not a tar, tar.gz, tar.zst or zip archive");
                }
                return postBatch(source, externalId, options);
            }
            return packAndPost(TarZst.collect(source), externalId, options);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    /** Submit the given image files as one batch (packed into a temporary .tar.zst). */
    public BatchSubmit submitBatch(List<Path> images, String externalId, SubmitOptions options) {
        try {
            return packAndPost(TarZst.of(images), externalId, options);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    private BatchSubmit packAndPost(List<TarZst.Entry> entries, String externalId,
                                    SubmitOptions options) throws IOException {
        if (entries.isEmpty()) throw new IllegalArgumentException("no images to submit");
        Path archive = Files.createTempFile("taas-batch-", ".tar.zst");
        try {
            TarZst.pack(entries, archive);
            return postBatch(archive, externalId, options);
        } finally {
            Files.deleteIfExists(archive);
        }
    }

    private BatchSubmit postBatch(Path archive, String externalId, SubmitOptions options)
            throws IOException {
        MultipartBody.Builder body = new MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            // streamed from disk, not read into memory
            .addFormDataPart("archive", archive.getFileName().toString(),
                RequestBody.create(archive.toFile(), MediaType.parse("application/octet-stream")))
            .addFormDataPart("outputs", String.join(",", options.effectiveOutputs()));
        if (externalId != null) {
            body.addFormDataPart("external_id", externalId);
        }
        addDomainAndOptions(body, options);

        Request request = new Request.Builder()
            .url(baseUrl + "/api/v1/batches")
            .header("X-API-Key", apiKey)
            .post(body.build())
            .build();
        // No read timeout: the server unpacks and stores every page before it answers.
        OkHttpClient uploads = http.newBuilder().readTimeout(0, TimeUnit.SECONDS).build();
        try (Response response = uploads.newCall(request).execute()) {
            if (!response.isSuccessful()) {
                throw new IOException("Batch submit failed: " + response.code()
                    + " " + response.body().string());
            }
            JsonObject obj = JsonParser.parseString(response.body().string()).getAsJsonObject();
            return new BatchSubmit(
                UUID.fromString(obj.get("batch_id").getAsString()),
                str(obj, "external_id"),
                obj.get("total").getAsInt(),
                obj.has("skipped") ? strings(obj.getAsJsonArray("skipped")) : List.of());
        }
    }

    /** The batch's progress; {@code pages} adds every page's status. */
    public BatchStatus getBatch(UUID batchId, boolean pages) {
        JsonObject b = getJson(baseUrl + "/api/v1/batches/" + batchId + (pages ? "?pages=1" : ""));
        List<BatchPage> pageList = null;
        if (b.has("pages") && !b.get("pages").isJsonNull()) {
            pageList = new ArrayList<>();
            for (JsonElement el : b.getAsJsonArray("pages")) {
                JsonObject p = el.getAsJsonObject();
                pageList.add(new BatchPage(
                    p.get("source_name").getAsString(),
                    UUID.fromString(p.get("job_id").getAsString()),
                    p.get("status").getAsString(),
                    str(p, "error")));
            }
        }
        return new BatchStatus(
            UUID.fromString(b.get("batch_id").getAsString()),
            str(b, "external_id"),
            b.get("status").getAsString(),
            str(b, "domain"),
            b.has("outputs") && !b.get("outputs").isJsonNull()
                ? strings(b.getAsJsonArray("outputs")) : List.of(),
            b.get("total").getAsInt(),
            intOr0(b, "queued"),
            intOr0(b, "running"),
            intOr0(b, "done"),
            intOr0(b, "failed"),
            str(b, "submitted_at"),
            str(b, "finished_at"),
            b.has("eta_s") && !b.get("eta_s").isJsonNull() ? b.get("eta_s").getAsDouble() : null,
            pageList);
    }

    /**
     * Block until the batch finishes and return its final status. Woken early by the
     * WebSocket progress events once {@link #start()} was called; polls every 5 s otherwise.
     */
    public BatchStatus awaitBatch(UUID batchId, long timeout, TimeUnit unit)
            throws InterruptedException, TimeoutException {
        long deadline = System.nanoTime() + unit.toNanos(timeout);
        try {
            while (true) {
                CountDownLatch waiter = new CountDownLatch(1);
                batchWaiters.put(batchId, waiter);
                BatchStatus status = getBatch(batchId, false);
                if (status.finished()) return status;
                long remaining = deadline - System.nanoTime();
                if (remaining <= 0) {
                    throw new TimeoutException("batch " + batchId + " not finished");
                }
                waiter.await(Math.min(remaining, TimeUnit.SECONDS.toNanos(5)), TimeUnit.NANOSECONDS);
            }
        } finally {
            batchWaiters.remove(batchId);
        }
    }

    /**
     * Download the batch's results archive into the {@code dest} directory, waiting while
     * the server builds it. With {@code extract}, also unpack it into
     * {@code dest/<batchId>/} and parse its {@code manifest.json}.
     */
    public BatchResults downloadBatchResults(UUID batchId, Path dest, boolean extract)
            throws InterruptedException {
        try {
            String url;
            while (true) {
                Request request = new Request.Builder()
                    .url(baseUrl + "/api/v1/batches/" + batchId + "/results")
                    .header("X-API-Key", apiKey)
                    .build();
                try (Response response = http.newCall(request).execute()) {
                    if (response.code() == 202) {
                        Thread.sleep(5000);  // the server is still building it
                        continue;
                    }
                    if (!response.isSuccessful()) {
                        throw new IOException("Batch results failed: " + response.code()
                            + " " + response.body().string());
                    }
                    url = JsonParser.parseString(response.body().string())
                        .getAsJsonObject().get("url").getAsString();
                    break;
                }
            }

            Files.createDirectories(dest);
            Path archive = dest.resolve(batchId + ".tar.zst");
            // The presigned URL carries its own auth; the API key isn't sent to storage.
            try (Response response = http.newCall(new Request.Builder().url(url).build()).execute()) {
                if (!response.isSuccessful()) {
                    throw new IOException("Download failed: " + response.code());
                }
                try (InputStream in = response.body().byteStream()) {
                    Files.copy(in, archive, StandardCopyOption.REPLACE_EXISTING);
                }
            }
            if (!extract) return new BatchResults(archive, null, null);
            Path directory = dest.resolve(batchId.toString());
            return new BatchResults(archive, directory, TarZst.extract(archive, directory, gson));
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    // ---- internals ----

    private void addDomainAndOptions(MultipartBody.Builder body, SubmitOptions options) {
        if (options.domain() != null && !options.domain().isEmpty()) {
            body.addFormDataPart("domain", options.domain());
        }
        if (options.options() != null && !options.options().isEmpty()) {
            body.addFormDataPart("options", gson.toJson(options.options()));
        }
    }

    private JsonObject getJson(String url) {
        Request request = new Request.Builder().url(url).header("X-API-Key", apiKey).build();
        try (Response response = http.newCall(request).execute()) {
            if (!response.isSuccessful()) {
                throw new IOException("GET " + url + " failed: " + response.code()
                    + " " + response.body().string());
            }
            return JsonParser.parseString(response.body().string()).getAsJsonObject();
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    private static String str(JsonObject obj, String key) {
        return obj.has(key) && !obj.get(key).isJsonNull() ? obj.get(key).getAsString() : null;
    }

    private static int intOr0(JsonObject obj, String key) {
        return obj.has(key) && !obj.get(key).isJsonNull() ? obj.get(key).getAsInt() : 0;
    }

    private static List<String> strings(JsonArray array) {
        List<String> out = new ArrayList<>();
        if (array != null) {
            for (JsonElement el : array) out.add(el.getAsString());
        }
        return out;
    }

    private void resolve(UUID jobId) {
        synchronized (pendingLock) {
            if (pending.remove(jobId)) {
                if (pending.isEmpty()) doneLatch.countDown();
                return;
            }
            // Not (yet) known: remember it in case its submit is still in flight.
            finishedEarly.put(jobId, Boolean.TRUE);
            if (finishedEarly.size() > FINISHED_EARLY_MAX) {
                finishedEarly.remove(finishedEarly.keySet().iterator().next());
            }
        }
    }

    private void connectWebSocket() {
        String wsUrl = baseUrl
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            + "/ws?api_key=" + apiKey;

        Request request = new Request.Builder().url(wsUrl).build();
        ws = http.newWebSocket(request, new TaasWebSocketListener());
    }

    private void reconnect() {
        if (!started) return;
        try {
            Thread.sleep(2000);
        } catch (InterruptedException ignored) {
            return;
        }
        connectWebSocket();
    }

    private void handleEvent(String json) throws IOException {
        JsonObject obj = JsonParser.parseString(json).getAsJsonObject();

        if ("batch".equals(str(obj, "type"))) {
            BatchProgress progress = new BatchProgress(
                UUID.fromString(obj.get("batch_id").getAsString()),
                obj.get("total").getAsInt(),
                obj.get("done").getAsInt(),
                obj.get("failed").getAsInt());
            BatchHandler handler = onBatch;
            if (handler != null) handler.onProgress(progress);
            CountDownLatch waiter = batchWaiters.get(progress.batchId());
            if (waiter != null && progress.finished()) waiter.countDown();
            return;
        }
        // Pages of a batch are downloaded as one archive; skip unknown events too.
        if (!obj.has("job_id") || !obj.has("status") || str(obj, "batch_id") != null) {
            return;
        }

        UUID jobId = UUID.fromString(obj.get("job_id").getAsString());
        String externalId = str(obj, "external_id");
        String status = obj.get("status").getAsString();

        if ("failed".equals(status)) {
            onError.onError(new JobEvent(
                jobId, externalId, status, null, null, str(obj, "error"), str(obj, "ts")));
            resolve(jobId);
            return;
        }

        if ("done".equals(status)) {
            byte[] alto = null;
            byte[] txt = null;

            if (str(obj, "alto_url") != null) {
                alto = fetchAndDecompress(obj.get("alto_url").getAsString());
            }
            if (str(obj, "txt_url") != null) {
                txt = fetchAndDecompress(obj.get("txt_url").getAsString());
            }

            onResult.onResult(new JobResult(jobId, externalId, alto, txt));
            resolve(jobId);
        }
    }

    private byte[] fetchAndDecompress(String url) throws IOException {
        Request request = new Request.Builder().url(url).build();
        try (Response response = http.newCall(request).execute()) {
            if (!response.isSuccessful()) {
                throw new IOException("Download failed: " + response.code());
            }
            byte[] compressed = response.body().bytes();
            long decompressedSize = Zstd.decompressedSize(compressed);
            if (decompressedSize > 0) {
                return Zstd.decompress(compressed, (int) decompressedSize);
            }
            // Fallback: streaming decompression
            return Zstd.decompress(compressed, compressed.length * 10);
        }
    }

    private static String guessMediaType(String filename) {
        String lower = filename.toLowerCase();
        if (lower.endsWith(".tiff") || lower.endsWith(".tif")) return "image/tiff";
        if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) return "image/jpeg";
        if (lower.endsWith(".png")) return "image/png";
        if (lower.endsWith(".jp2")) return "image/jp2";
        return "application/octet-stream";
    }

    private class TaasWebSocketListener extends WebSocketListener {

        @Override
        public void onMessage(WebSocket webSocket, String text) {
            try {
                handleEvent(text);
            } catch (Exception e) {
                // Log but don't crash the WS thread
                System.err.println("Error handling WS event: " + e.getMessage());
            }
        }

        @Override
        public void onFailure(WebSocket webSocket, Throwable t, Response response) {
            if (started) {
                reconnect();
            }
        }

        @Override
        public void onClosed(WebSocket webSocket, int code, String reason) {
            if (started && code != 1000) {
                reconnect();
            }
        }
    }
}
