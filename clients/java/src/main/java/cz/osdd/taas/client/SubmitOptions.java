package cz.osdd.taas.client;

import java.util.List;
import java.util.Map;

/**
 * What a job (or batch) asks for: domain, outputs and engine options. Immutable; build it
 * with the {@code with*} methods. {@code outputs} replaces {@code fmt} when set, and
 * {@code options} is validated by the server against the domain's options schema (see
 * {@link TaasClient#domains()}).
 *
 * <pre>{@code
 * SubmitOptions.defaults().withDomain("kramarky").withOutputs("alto", "txt")
 *     .withOptions(Map.of("alto_profile", "basic"))
 * }</pre>
 */
public record SubmitOptions(
    String fmt,
    String domain,
    List<String> outputs,
    Map<String, Object> options
) {
    public SubmitOptions {
        outputs = outputs == null ? null : List.copyOf(outputs);
        options = options == null ? null : Map.copyOf(options);
    }

    /** Legacy {@code fmt=multi} (ALTO + text), the server's default domain, no options. */
    public static SubmitOptions defaults() {
        return new SubmitOptions("multi", null, null, null);
    }

    public SubmitOptions withFmt(String fmt) {
        return new SubmitOptions(fmt, domain, outputs, options);
    }

    public SubmitOptions withDomain(String domain) {
        return new SubmitOptions(fmt, domain, outputs, options);
    }

    public SubmitOptions withOutputs(String... outputs) {
        return new SubmitOptions(fmt, domain, List.of(outputs), options);
    }

    public SubmitOptions withOptions(Map<String, Object> options) {
        return new SubmitOptions(fmt, domain, outputs, options);
    }

    /** The outputs to request: {@code outputs} if set, else what {@code fmt} stands for. */
    List<String> effectiveOutputs() {
        if (outputs != null) return outputs;
        return switch (fmt == null ? "multi" : fmt) {
            case "alto" -> List.of("alto");
            case "txt" -> List.of("txt");
            default -> List.of("alto", "txt");
        };
    }
}
