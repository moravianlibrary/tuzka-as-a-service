package cz.osdd.taas.client;

import java.util.UUID;

/** One page of a batch, as listed by {@code getBatch(id, true)}. */
public record BatchPage(
    String sourceName,
    UUID jobId,
    UUID externalId,
    String status,
    String error
) {}
