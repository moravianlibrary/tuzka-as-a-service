package cz.osdd.taas.client;

import java.util.List;
import java.util.UUID;

/** A batch's progress ({@code GET /api/v1/batches/{id}}). {@code pages} is null unless asked for. */
public record BatchStatus(
    UUID batchId,
    String status,
    String domain,
    List<String> outputs,
    int total,
    int queued,
    int running,
    int done,
    int failed,
    String submittedAt,
    String finishedAt,
    /** A rough server-side estimate of the seconds left; null if unknown. */
    Double etaSeconds,
    List<BatchPage> pages
) {
    public boolean finished() {
        return "finished".equals(status);
    }
}
