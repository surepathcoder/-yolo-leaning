# Event evidence

When a significant event fires, an operator later wants to *see* it, not just
read a summary. VisionGuard captures three artefacts per such event:

| Kind | What | Retention |
|------|------|-----------|
| `SNAPSHOT` | The full frame at the moment, annotated with a box around the track the event concerns | `SNAPSHOT_RETENTION_DAYS` (30) |
| `THUMBNAIL` | A small version of the snapshot, for the timeline | `SNAPSHOT_RETENTION_DAYS` (30) |
| `CLIP` | An MP4 covering `EVIDENCE_PRE_SECONDS` before to `EVIDENCE_POST_SECONDS` after | `EVIDENCE_RETENTION_DAYS` (30) |

## When capture happens

Only for events that would raise an alert: severity **MEDIUM or above**, and
not a high-volume type (line crossings, bare detections, zone exits). An
ordinary afternoon of trading produces no evidence at all.

`vision/evidence/policy.py::should_capture` is the single predicate. It is a
deliberate mirror of `backend/app/services/alerts.py::should_alert`; a
parametrised test (`test_vision_evidence.py`) asserts the two agree for every
event type and severity, the same way `INFERENCE_EVENT_TYPES` is mirrored
between the vision and backend layers.

## How it is produced

`vision/evidence/` is pure: it turns frames into `bytes` and knows nothing
about disk or the database.

- `EvidenceCapturer` keeps a rolling buffer of the last `pre_seconds` of
  decoded frames. On a triggering event it emits the snapshot and thumbnail
  immediately, then opens a *pending clip* that collects `post_seconds` more
  frames before encoding.
- Encoding is best-effort. A missing codec or a bad frame increments a counter
  and drops that one artefact — it never raises into the pipeline thread, on
  the same principle as `CallbackSink`.
- `flush()` (called on shutdown) finalises any clip still filling.

The backend wires it in `pipeline_manager`: a per-pipeline single-thread
executor receives each artefact **off the frame thread**, matches it to the
already-persisted `Event` by dedupe key, writes the file under
`EVIDENCE_STORAGE_PATH`, and links an `EventEvidence` row with size, checksum
and `expires_at`.

Storage layout: `<org>/<yyyy>/<mm>/<dd>/<event>/<kind>-<rand>.<ext>`.

## Serving it

`GET /api/v1/evidence/{id}` — requires `evidence:view`, tenant-scoped through
the parent event, audited as `EVIDENCE_ACCESSED`, served from disk.
`GET /api/v1/evidence/{id}/download` — additionally requires `evidence:export`,
audited as `EXPORT`. `ANALYST` holds neither: analysing traffic patterns does
not require looking at pictures of people.

## Retention

Every row has an `expires_at` set on creation. The retention sweep
(`RETENTION_SWEEP_ENABLED`, every `RETENTION_SWEEP_INTERVAL_HOURS`; also
`POST /api/v1/system/maintenance/retention` on demand) deletes expired rows and
their files. It also prunes long-expired refresh tokens. Event pruning is *not*
part of it — that is per-organization and governed by plan limits.

## Configuration

| Setting | Default | Effect |
|---------|---------|--------|
| `EVIDENCE_CAPTURE_ENABLED` | `true` | Master switch |
| `EVIDENCE_CLIP_ENABLED` | `true` | Capture clips as well as stills |
| `EVIDENCE_PRE_SECONDS` / `EVIDENCE_POST_SECONDS` | `4.0` | Clip window |
| `EVIDENCE_CLIP_FPS` | `8.0` | Frame rate written into the container |
| `EVIDENCE_THUMBNAIL_WIDTH` | `320` | Thumbnail width in pixels |

## What it deliberately does not do

- **No continuous recording.** Only alert-worthy moments, and only a few
  seconds each. VisionGuard is not an NVR.
- **No evidence for inferences' sources by default** beyond what those source
  observations captured on their own merits.
- **No public or guessable URLs.** Every byte served is authorised and audited.
