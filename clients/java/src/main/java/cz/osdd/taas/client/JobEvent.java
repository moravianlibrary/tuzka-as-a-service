package cz.osdd.taas.client;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.util.UUID;

/**
 * Raw event received from WebSocket.
 */
public record JobEvent(
    UUID uuid,
    String status,
    String altoUrl,
    String txtUrl,
    String error,
    String ts
) {
    public static JobEvent fromJson(String json) {
        JsonObject obj = JsonParser.parseString(json).getAsJsonObject();
        UUID uuid = UUID.fromString(obj.get("uuid").getAsString());
        String status = obj.get("status").getAsString();
        String altoUrl = obj.has("alto_url") && !obj.get("alto_url").isJsonNull()
            ? obj.get("alto_url").getAsString() : null;
        String txtUrl = obj.has("txt_url") && !obj.get("txt_url").isJsonNull()
            ? obj.get("txt_url").getAsString() : null;
        String error = obj.has("error") && !obj.get("error").isJsonNull()
            ? obj.get("error").getAsString() : null;
        String ts = obj.has("ts") && !obj.get("ts").isJsonNull()
            ? obj.get("ts").getAsString() : null;
        return new JobEvent(uuid, status, altoUrl, txtUrl, error, ts);
    }
}
