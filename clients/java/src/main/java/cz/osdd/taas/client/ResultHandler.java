package cz.osdd.taas.client;

/**
 * Callback invoked when OCR results are ready.
 * Called from the WebSocket listener thread.
 */
@FunctionalInterface
public interface ResultHandler {
    void onResult(JobResult result);
}
