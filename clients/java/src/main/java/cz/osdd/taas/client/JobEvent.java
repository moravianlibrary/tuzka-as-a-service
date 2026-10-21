package cz.osdd.taas.client;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.util.UUID;

/**
 * Raw event received from WebSocket. {@code externalId} is the label the page was
 * submitted with (null if none).
 */
public record JobEvent(
    UUID jobId,
    String externalId,
    String status,
    String altoUrl,
    String txtUrl,
    String error,
    String ts
) {
    public static JobEvent fromJson(String json) {
        JsonObject obj = JsonParser.parseString(json).getAsJsonObject();
        return new JobEvent(
            UUID.fromString(obj.get("job_id").getAsString()),
            str(obj, "external_id"),
            obj.get("status").getAsString(),
            str(obj, "alto_url"),
            str(obj, "txt_url"),
            str(obj, "error"),
            str(obj, "ts"));
    }

    private static String str(JsonObject obj, String key) {
        return obj.has(key) && !obj.get(key).isJsonNull() ? obj.get(key).getAsString() : null;
    }
}
