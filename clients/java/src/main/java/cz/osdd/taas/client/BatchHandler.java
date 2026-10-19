package cz.osdd.taas.client;

/**
 * Callback for batch progress events. Called on the WebSocket thread.
 */
@FunctionalInterface
public interface BatchHandler {
    void onProgress(BatchProgress progress);
}
