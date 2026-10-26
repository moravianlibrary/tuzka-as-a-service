package cz.osdd.taas.client;

import java.nio.file.Path;
import java.util.List;
import java.util.Map;

/**
 * A downloaded batch result. {@code directory} and {@code manifest} are set only when it
 * was extracted; the manifest has one entry per page (source_name, status, outputs, error).
 */
public record BatchResults(Path archive, Path directory, List<Map<String, Object>> manifest) {}
