package cz.osdd.taas.client;

import com.github.luben.zstd.Zstd;
import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import okhttp3.*;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Thread-safe taas client with callback-driven result delivery.
 * Designed for single start()/stop() lifecycle.
 */
public class TaasClient implements AutoCloseable {

    private final String baseUrl;
    private final String apiKey;
    private final ResultHandler onResult;
    private final ErrorHandler onError;
    private final String defaultFmt;
    private final String defaultDomain;

    private final OkHttpClient http;
    private final Gson gson;

    private final Set<UUID> pending = ConcurrentHashMap.newKeySet();
    private final CountDownLatch doneLatch = new CountDownLatch(1);
    private volatile boolean started = false;
    private volatile WebSocket ws;

    public TaasClient(
        String url,
        String apiKey,
        ResultHandler onResult,
        ErrorHandler onError
    ) {
        this(url, apiKey, onResult, onError, "multi", null);
    }

    public TaasClient(
        String url,
        String apiKey,
        ResultHandler onResult,
        ErrorHandler onError,
        String fmt,
        String domain
    ) {
        this.baseUrl = url.endsWith("/") ? url.substring(0, url.length() - 1) : url;
        this.apiKey = apiKey;
        this.onResult = onResult;
        this.onError = onError;
        this.defaultFmt = fmt;
        this.defaultDomain = domain;
        this.http = new OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .build();
        this.gson = new Gson();
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

    public UUID submit(Path imagePath) {
        return submit(imagePath, imagePath.getFileName().toString(), defaultFmt, defaultDomain);
    }

    public UUID submit(Path imagePath, String filename, String fmt, String domain) {
        UUID uuid = UUID.randomUUID();
        pending.add(uuid);

        String mediaType = guessMediaType(filename);
        byte[] imageBytes;
        try {
            imageBytes = Files.readAllBytes(imagePath);
        } catch (IOException e) {
            pending.remove(uuid);
            throw new UncheckedIOException(e);
        }

        MultipartBody.Builder bodyBuilder = new MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("image", filename,
                RequestBody.create(imageBytes, MediaType.parse(mediaType)))
            .addFormDataPart("uuid", uuid.toString())
            .addFormDataPart("fmt", fmt);

        if (domain != null && !domain.isEmpty()) {
            bodyBuilder.addFormDataPart("domain", domain);
        }

        Request request = new Request.Builder()
            .url(baseUrl + "/api/v1/jobs")
            .header("X-API-Key", apiKey)
            .post(bodyBuilder.build())
            .build();

        try (Response response = http.newCall(request).execute()) {
            if (!response.isSuccessful()) {
                pending.remove(uuid);
                throw new IOException("Submit failed: " + response.code()
                    + " " + response.body().string());
            }
        } catch (IOException e) {
            pending.remove(uuid);
            throw new UncheckedIOException(e);
        }

        return uuid;
    }

    public boolean awaitAll() throws InterruptedException {
        return awaitAll(Long.MAX_VALUE, TimeUnit.MILLISECONDS);
    }

    public boolean awaitAll(long timeout, TimeUnit unit) throws InterruptedException {
        if (pending.isEmpty()) return true;
        return doneLatch.await(timeout, unit);
    }

    private void resolve(UUID uuid) {
        pending.remove(uuid);
        if (pending.isEmpty()) {
            doneLatch.countDown();
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
        UUID uuid = UUID.fromString(obj.get("uuid").getAsString());
        String status = obj.get("status").getAsString();

        if ("failed".equals(status)) {
            String error = obj.has("error") && !obj.get("error").isJsonNull()
                ? obj.get("error").getAsString() : null;
            String ts = obj.has("ts") && !obj.get("ts").isJsonNull()
                ? obj.get("ts").getAsString() : null;
            onError.onError(new JobEvent(uuid, status, null, null, error, ts));
            resolve(uuid);
            return;
        }

        if ("done".equals(status)) {
            byte[] alto = null;
            byte[] txt = null;

            if (obj.has("alto_url") && !obj.get("alto_url").isJsonNull()) {
                alto = fetchAndDecompress(obj.get("alto_url").getAsString());
            }
            if (obj.has("txt_url") && !obj.get("txt_url").isJsonNull()) {
                txt = fetchAndDecompress(obj.get("txt_url").getAsString());
            }

            onResult.onResult(new JobResult(uuid, alto, txt));
            resolve(uuid);
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
