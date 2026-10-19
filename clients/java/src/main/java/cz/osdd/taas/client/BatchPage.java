package cz.osdd.taas.client;

import java.util.UUID;

/**
 * One page of a batch, as listed by {@code getBatch(id, true)}. {@code sourceName} is the
 * page's path inside the archive you sent.
 */
public record BatchPage(
    String sourceName,
    UUID jobId,
    String status,
    String error
) {}
