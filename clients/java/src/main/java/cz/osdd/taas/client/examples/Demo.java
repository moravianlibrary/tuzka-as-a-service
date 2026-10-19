package cz.osdd.taas.client.examples;

import com.google.gson.Gson;
import cz.osdd.taas.client.BatchResults;
import cz.osdd.taas.client.BatchStatus;
import cz.osdd.taas.client.BatchSubmit;
import cz.osdd.taas.client.DomainInfo;
import cz.osdd.taas.client.SubmitOptions;
import cz.osdd.taas.client.TaasClient;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import java.util.stream.Stream;

/**
 * taas client demo: the same directory of images, OCR'd both ways.
 * <ol>
 *   <li><b>Per page</b> — submit every image as its own job, labelled with your own page
 *       id ({@code externalId}, here the file name without extension); results arrive
 *       over the WebSocket as each page finishes, carrying that label. Best for
 *       interactive use and small sets.</li>
 *   <li><b>As one batch</b> — pack the same images into a .tar.zst and submit it once,
 *       labelled with your id for the whole batch (here the directory name). Watch the
 *       progress events, then download one archive with every page's output; its
 *       manifest maps each page back by its file name ({@code source_name}). Best for
 *       large sets.</li>
 * </ol>
 *
 * <pre>
 * mvn -q compile exec:java -Dexec.args="http://localhost:8080 KEY ../../test-data"
 * mvn -q compile exec:java -Dexec.args="URL KEY DIR kramarky alto,txt {\"alto_profile\":\"basic\"}"
 * </pre>
 * Arguments: url, api key, image directory, then optionally domain, outputs, options (JSON).
 */
public final class Demo {

    private static final Set<String> IMAGE_SUFFIXES =
        Set.of(".jpg", ".jpeg", ".png", ".tif", ".tiff", ".jp2");

    public static void main(String[] args) throws Exception {
        if (args.length < 3) {
            System.err.println("usage: Demo <url> <api-key> <image-dir> [domain] [outputs] [options-json]");
            System.exit(2);
        }
        String url = args[0];
        String apiKey = args[1];
        Path dir = Path.of(args[2]);

        SubmitOptions options = SubmitOptions.defaults();
        if (args.length > 3 && !args[3].isEmpty()) options = options.withDomain(args[3]);
        if (args.length > 4 && !args[4].isEmpty()) options = options.withOutputs(args[4].split(","));
        if (args.length > 5 && !args[5].isEmpty()) {
            @SuppressWarnings("unchecked")
            Map<String, Object> engineOptions = new Gson().fromJson(args[5], Map.class);
            options = options.withOptions(engineOptions);
        }

        List<Path> images = listImages(dir);
        if (images.isEmpty()) {
            System.err.println("no images in " + dir);
            System.exit(2);
        }
        Path out = Files.createTempDirectory("taas-demo-");
        Path pageDir = Files.createDirectories(out.resolve("per-page"));

        try (TaasClient client = new TaasClient(
                url, apiKey,
                result -> {
                    // the label we submitted the page with
                    String name = result.externalId() != null
                        ? result.externalId() : result.jobId().toString();
                    try {
                        if (result.alto() != null) {
                            Files.write(pageDir.resolve(name + ".xml"), result.alto());
                        }
                        if (result.txt() != null) {
                            Files.write(pageDir.resolve(name + ".txt"), result.txt());
                        }
                    } catch (IOException e) {
                        System.err.println("  could not write " + name + ": " + e.getMessage());
                    }
                    System.out.println("  page " + name + " done");
                },
                event -> System.out.println("  page "
                    + (event.externalId() != null ? event.externalId() : event.jobId())
                    + " FAILED: " + event.error()),
                options)) {

            client.onBatchProgress(p ->
                System.out.println("  batch progress: " + (p.done() + p.failed()) + "/" + p.total()));
            client.start();

            listDomains(client);
            double pageSeconds = perPage(client, images, options, pageDir);
            String batchLabel = dir.toAbsolutePath().normalize().getFileName().toString();
            double batchSeconds = asBatch(client, images, batchLabel, options, out.resolve("batch"));

            System.out.printf("%n%d page(s): per page %.1fs, as one batch %.1fs%n",
                images.size(), pageSeconds, batchSeconds);
        }
    }

    private static void listDomains(TaasClient client) {
        System.out.println("Domains you can submit to:");
        for (DomainInfo d : client.domains()) {
            Object props = d.optionsSchema() == null ? null : d.optionsSchema().get("properties");
            String opts = props instanceof Map<?, ?> m && !m.isEmpty()
                ? String.join(", ", m.keySet().stream().map(Object::toString).sorted().toList())
                : "-";
            System.out.printf("  %-12s outputs=%-12s options=%s%n",
                d.name(), String.join(",", d.outputs()), opts);
        }
    }

    /** Submit each image as a job; the WebSocket callbacks write the results. */
    private static double perPage(TaasClient client, List<Path> images, SubmitOptions options,
                                  Path out) throws InterruptedException {
        System.out.println("\n== Per page: " + images.size() + " job(s)");
        long started = System.nanoTime();
        for (Path image : images) {
            client.submit(image, stem(image), options);  // your id for this page
        }
        if (!client.awaitAll(1, TimeUnit.HOURS)) {
            System.out.println("  timed out waiting for results");
        }
        double seconds = (System.nanoTime() - started) / 1e9;
        System.out.printf("  done in %.1fs, results in %s%n", seconds, out);
        return seconds;
    }

    /** Submit the same images as one batch and download one results archive. */
    private static double asBatch(TaasClient client, List<Path> images, String label,
                                  SubmitOptions options, Path out) throws Exception {
        System.out.println("\n== As one batch");
        long started = System.nanoTime();
        BatchSubmit batch = client.submitBatch(images, label, options);  // your id for the batch
        System.out.println("  batch " + batch.batchId() + " (" + batch.externalId() + "): "
            + batch.total() + " page(s)"
            + (batch.skipped().isEmpty() ? "" : ", skipped " + String.join(", ", batch.skipped())));

        BatchStatus status = client.awaitBatch(batch.batchId(), 1, TimeUnit.HOURS);
        System.out.println("  finished: " + status.done() + " done, " + status.failed() + " failed");

        BatchResults results = client.downloadBatchResults(batch.batchId(), out, true);
        double seconds = (System.nanoTime() - started) / 1e9;
        System.out.printf("  done in %.1fs, archive %s, extracted to %s%n",
            seconds, results.archive(), results.directory());
        List<Map<String, Object>> manifest = results.manifest() == null ? List.of() : results.manifest();
        for (Map<String, Object> entry : manifest.subList(0, Math.min(5, manifest.size()))) {
            Object files = entry.get("outputs") instanceof Map<?, ?> m && !m.isEmpty()
                ? String.join(", ", m.values().stream().map(Object::toString).toList())
                : entry.get("error");
            System.out.println("    " + entry.get("source_name") + ": " + entry.get("status") + " -> " + files);
        }
        if (manifest.size() > 5) {
            System.out.println("    … " + (manifest.size() - 5) + " more in manifest.json");
        }
        return seconds;
    }

    private static String stem(Path file) {
        String name = file.getFileName().toString();
        int dot = name.lastIndexOf('.');
        return dot > 0 ? name.substring(0, dot) : name;
    }

    private static List<Path> listImages(Path dir) throws IOException {
        try (Stream<Path> files = Files.list(dir)) {
            return files.filter(Files::isRegularFile)
                .filter(p -> {
                    String name = p.getFileName().toString().toLowerCase();
                    int dot = name.lastIndexOf('.');
                    return dot >= 0 && IMAGE_SUFFIXES.contains(name.substring(dot));
                })
                .sorted()
                .toList();
        }
    }
}
