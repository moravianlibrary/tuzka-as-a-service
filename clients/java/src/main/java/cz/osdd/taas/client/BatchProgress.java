package cz.osdd.taas.client;

import java.util.UUID;

/** A batch progress event pushed over the WebSocket. */
public record BatchProgress(UUID batchId, int total, int done, int failed) {
    public boolean finished() {
        return done + failed >= total;
    }
}
