package cz.osdd.taas.client;

import java.util.List;
import java.util.UUID;

/** The server's answer to a batch upload. {@code skipped} lists members that weren't images. */
public record BatchSubmit(UUID batchId, int total, List<String> skipped) {}
