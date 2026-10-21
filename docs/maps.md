# Maps (domain `maps`, TuzkaM)

Historical map sheets go through [TuzkaM](https://gitlab.mzk.cz/osdd/tuzkam), a separate
engine that reads the labels on a sheet and can georeference it against a gazetteer.
taas reaches it through the `tuzkam` adapter; a job asks for it with `domain=maps`.

## Outputs

| Output | What | Needs georeferencing |
|---|---|---|
| `json` | TuzkaM's full result: every word with its box and confidence, the georef summary (`schema_version` inside) | no |
| `alto` | ALTO XML of the words (`alto_profile` picks the profile) | no |
| `txt` | plain text of the words | no |
| `geojson` | the words as GeoJSON features with approximate coordinates | **yes** |
| `gcp` | ground control points for QGIS's georeferencer | **yes** |

```bash
curl -H "X-API-Key: $KEY" -F image=@sheet.jpg -F domain=maps \
  -F outputs=json,geojson,alto -F 'options={"region": "CZ"}' \
  "$TAAS/api/v1/jobs"
```

Editing the result (e.g. building MODS records from the GeoJSON) is the client's job.

## Options

Sent as the `options` JSON object; `GET /api/v1/domains` shows the schema taas validates
them against.

| Option | Type | Default | Meaning |
|---|---|---|---|
| `georef` | boolean | `true` | Match the labels against the gazetteer |
| `region` | string | — | Limit the gazetteer to a region (TuzkaM validates the value) |
| `alto_profile` | `ndk` \| `basic` | `ndk` | ALTO flavour |
| `source_identifier` | string | — | Goes into the ALTO header |
| `physical_img_nr` | integer ≥ 1 | — | Goes into the ALTO header |

Asking for `geojson` or `gcp` with `georef: false` is a `422` at submit: they can't exist
without georeferencing.

## When georeferencing fails, the job fails

A sheet can be read fine and still not georeference (too few labels matched the
gazetteer). TuzkaM then can't produce `gcp` (and `geojson` needs a georeferenced job).
If the job asked for such an output, **the whole job fails**, with an error like
`gcp unavailable: Georeferencing failed; QGIS points are unavailable`, and it isn't
retried: another attempt gives the same answer. A job delivers everything it asked for
or nothing, so a client never has to check which outputs it actually got. To get the
text of a sheet that may not georeference, ask for `json`, `alto` or `txt` only.

## Limits

| | maps | everything else |
|---|---|---|
| Upload size | 150 MiB | `MAX_UPLOAD_BYTES` (100 MB) |
| Running timeout | 1800 s | `jobs.running_timeout_seconds` (300 s) |
| Image size | 400 megapixels (TuzkaM's own; larger fails, not retried) | — |

The first two are per-domain columns (`domains.max_upload_bytes`,
`domains.running_timeout_seconds`, `NULL` = the global value); migration 016 sets them
for `maps`. To change them before the admin CLI exists:

```sql
UPDATE domains SET max_upload_bytes = 209715200, running_timeout_seconds = 3600
WHERE name = 'maps';
```

## Running it

- **Locally:** `docker compose --profile maps up -d tuzkam` builds it from a TuzkaM
  checkout (`TUZKAM_DIR`, default `../../../gitlab.mzk.cz/osdd/tuzkam`); `make seed`
  registers it.
- **Kubernetes:** `tuzkam.enabled=true` in the Helm chart: a StatefulSet that KEDA
  scales from zero on `/load?adapter=tuzkam` (see the chart README). Set
  `jobs.queued_timeout_seconds` to at least twice TuzkaM's cold start (35–44 s on k3d
  with the image cached; the 900 s default covers it), or sheets submitted while it's
  at zero time out before the first pod is ready.

TuzkaM doesn't advertise domains itself; the adapter reports the ones in the
`TUZKAM_DOMAINS` setting (default `["maps"]`).
