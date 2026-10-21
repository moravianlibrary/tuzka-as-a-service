package cz.osdd.taas.client;

import java.util.UUID;

/**
 * Decompressed OCR result delivered to ResultHandler callback. {@code externalId} is the
 * label the page was submitted with (null if none).
 */
public record JobResult(
    UUID jobId,
    String externalId,
    byte[] alto,
    byte[] txt
) {}
