package cz.osdd.taas.client;

/**
 * Callback invoked when a job fails.
 * Called from the WebSocket listener thread.
 */
@FunctionalInterface
public interface ErrorHandler {
    void onError(JobEvent event);
}
