package cz.osdd.taas.client;

import java.util.UUID;

/**
 * Decompressed OCR result delivered to ResultHandler callback.
 */
public record JobResult(
    UUID uuid,
    byte[] alto,
    byte[] txt
) {}
