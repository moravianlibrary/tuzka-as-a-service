package cz.osdd.taas.client;

import com.github.luben.zstd.ZstdInputStream;
import com.github.luben.zstd.ZstdOutputStream;
import com.google.gson.Gson;
import com.google.gson.reflect.TypeToken;
import org.apache.commons.compress.archivers.tar.TarArchiveEntry;
import org.apache.commons.compress.archivers.tar.TarArchiveInputStream;
import org.apache.commons.compress.archivers.tar.TarArchiveOutputStream;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

/** Packing and unpacking {@code .tar.zst} archives, one file at a time (never all in memory). */
final class TarZst {

    private static final byte[] ZSTD = {0x28, (byte) 0xB5, 0x2F, (byte) 0xFD};
    private static final byte[] GZIP = {0x1F, (byte) 0x8B};
    private static final byte[] ZIP = {'P', 'K', 3, 4};
    private static final byte[] USTAR = {'u', 's', 't', 'a', 'r'};

    private static final java.lang.reflect.Type MANIFEST_TYPE =
        new TypeToken<List<Map<String, Object>>>() {}.getType();

    private TarZst() {}

    /** A file to pack, and its name inside the archive. */
    record Entry(Path path, String name) {}

    /** Whether {@code path} is an archive the batch API accepts (the magic bytes it checks). */
    static boolean isArchive(Path path) throws IOException {
        byte[] head = new byte[262];
        int n;
        try (InputStream in = Files.newInputStream(path)) {
            n = in.readNBytes(head, 0, head.length);
        }
        return startsWith(head, n, ZSTD) || startsWith(head, n, GZIP) || startsWith(head, n, ZIP)
            || (n >= 262 && Arrays.equals(Arrays.copyOfRange(head, 257, 262), USTAR));
    }

    private static boolean startsWith(byte[] head, int n, byte[] magic) {
        return n >= magic.length && Arrays.equals(Arrays.copyOf(head, magic.length), magic);
    }

    /** Every regular file under {@code dir}, named relative to it, in a stable order. */
    static List<Entry> collect(Path dir) throws IOException {
        try (Stream<Path> walk = Files.walk(dir)) {
            return walk.filter(Files::isRegularFile)
                .sorted()
                .map(p -> new Entry(p, dir.relativize(p).toString().replace('\\', '/')))
                .toList();
        }
    }

    /** The given files, named by their file names. */
    static List<Entry> of(List<Path> files) {
        return files.stream().map(p -> new Entry(p, p.getFileName().toString())).toList();
    }

    static void pack(List<Entry> entries, Path dest) throws IOException {
        try (OutputStream file = new BufferedOutputStream(Files.newOutputStream(dest));
             ZstdOutputStream zst = new ZstdOutputStream(file);
             TarArchiveOutputStream tar = new TarArchiveOutputStream(zst)) {
            tar.setLongFileMode(TarArchiveOutputStream.LONGFILE_POSIX);
            for (Entry e : entries) {
                TarArchiveEntry entry = new TarArchiveEntry(e.path().toFile(), e.name());
                tar.putArchiveEntry(entry);
                Files.copy(e.path(), tar);
                tar.closeArchiveEntry();
            }
            tar.finish();
        }
    }

    /**
     * Unpack {@code archive} into {@code dest}, refusing entries that would land outside it.
     * Returns the parsed {@code manifest.json} if the archive has one, else null.
     */
    static List<Map<String, Object>> extract(Path archive, Path dest, Gson gson)
            throws IOException {
        Files.createDirectories(dest);
        Path root = dest.toAbsolutePath().normalize();
        try (InputStream file = new BufferedInputStream(Files.newInputStream(archive));
             ZstdInputStream zst = new ZstdInputStream(file);
             TarArchiveInputStream tar = new TarArchiveInputStream(zst)) {
            TarArchiveEntry entry;
            while ((entry = tar.getNextEntry()) != null) {
                Path target = root.resolve(entry.getName()).normalize();
                if (!target.startsWith(root)) {
                    throw new IOException("Archive entry outside the destination: " + entry.getName());
                }
                if (entry.isDirectory()) {
                    Files.createDirectories(target);
                } else if (entry.isFile()) {
                    Files.createDirectories(target.getParent());
                    Files.copy(tar, target, StandardCopyOption.REPLACE_EXISTING);
                }
                // links and special files are skipped
            }
        }
        Path manifest = root.resolve("manifest.json");
        if (!Files.isRegularFile(manifest)) return null;
        return gson.fromJson(Files.readString(manifest), MANIFEST_TYPE);
    }
}
