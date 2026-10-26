package cz.osdd.taas.client;

import java.util.List;
import java.util.Map;

/** A domain you can submit to ({@code GET /api/v1/domains}). */
public record DomainInfo(
    String name,
    String label,
    String description,
    List<String> outputs,
    /** JSON Schema of the options this domain accepts; null = none yet. */
    Map<String, Object> optionsSchema,
    Integer legacyEngineId
) {}
