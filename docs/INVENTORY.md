# Inventory

How VisionGuard estimates what is on a shelf, and — more importantly — when it
refuses to guess.

## What is actually measured

**Coverage.** The fraction of a shelf region that has something on it.

That is all. Not a unit count, not a value, not a product identity beyond the
class name the detector emits.

This is a deliberate ceiling, not a missing feature. A camera looking along an
aisle at a packed shelf cannot resolve individual items: the boxes at the back
are occluded by the boxes at the front, the front row occludes itself, and
depth is unrecoverable from one view. A system that reported "17 units" would
be confidently wrong most of the time, and every number downstream — reorder
points, shrinkage figures, discrepancy alerts — would inherit that error
without inheriting the uncertainty.

A coverage ratio degrades honestly. When the view is bad, it is wrong by a
little. An invented integer is wrong by an unknowable amount and looks exactly
as authoritative as a correct one.

## How coverage is computed

```
shelf polygon ──► 64x64 sample grid, clipped to the polygon
                            │
product detections ─────────┼──► union mask  ──► covered cells / visible cells
occluder detections ────────┘                            │
                                                   raw_coverage
                                                         │
                                          ÷ baseline_occupancy (if calibrated)
                                                         │
                                                  occupancy_ratio
                                                         │
                                            thresholds ──► OccupancyLevel
```

Four properties of that pipeline are load-bearing.

**The grid is fixed, never sampled randomly.** Identical input must produce an
identical ratio. Occupancy is compared against thresholds over time, so a value
that jitters by a few percent between identical frames would manufacture stock
events out of nothing.

**The grid is clipped to the polygon, not its bounding box.** Installers draw
L-shapes and angled shelves constantly. Measuring the bounding rectangle would
count the aisle floor as shelf.

**Coverage is a union, not a sum.** Two detections overlapping the same patch
describe one covered patch. Adding their areas would report a shelf as more
than full, and clamping that to 1.0 would hide the bug rather than fix it.

**Coverage is measured over the *visible* shelf.** Cells hidden by an occluder
are excluded from both numerator and denominator. A shelf half hidden by a
shopper, whose visible half is fully stocked, reads as stocked — not as half
empty.

### Baseline calibration

A real shelf that is completely full never reads 1.0. Gaps between items,
shadows between shelves, and the shelf edge itself all read as uncovered.
Typical raw coverage for a full shelf is 0.5–0.8 depending on the angle.

`baseline_occupancy` is the coverage seen when the shelf is known full.
Everything after is divided by it, so a shelf reading 0.6 against a 0.6
baseline is 100% full rather than 60%.

Calibrate with `calibrate_baseline()`, which takes the **median** of a set of
readings, not the maximum. One frame where a reflection inflated coverage would
otherwise become the baseline permanently, and every later reading would be
scaled down against it.

Until a shelf is calibrated, raw coverage is reported uncorrected. That is
wrong in a known direction — it under-reports fullness — which is preferable to
being wrong in an unknown one.

## Occlusion: the rule the feature rests on

A person standing at a shelf hides it. Reported naively, that shelf reads
empty.

This is not an edge case. It happens every time a customer shops, which is
precisely when someone is most likely to be looking at the dashboard. A system
that announces `SHELF_EMPTY` every time somebody stands at the shelf trains
staff to ignore stock alerts within a day, and after that the real stockouts go
unread too.

The honest answer when a shelf is hidden is **UNKNOWN**, never **EMPTY**. That
is enforced at four layers:

| Layer | Behaviour |
|-------|-----------|
| `estimate_occupancy` | Occluders (`person`, `car`, `truck`, `forklift` by default) are measured on the same grid. Above `occlusion_threshold` (default 0.25 of the shelf) the reading is marked occluded, its level is `UNKNOWN` and its confidence is 0 |
| `InventoryEngine` | Occluded readings never move shelf state and never emit. They also **clear any partial confirmation**, so readings taken either side of a passer-by are not stitched into a false "stable" sequence |
| `services/inventory` | Occluded snapshots are stored — they are a true record of what the camera saw, and dropping them would leave an unexplained gap in the history exactly when someone asks what happened — but they never establish status and never count towards "empty" |
| Dashboard | An unknown shelf gets **no progress bar at all**. A zero-width bar and a genuinely empty shelf would look identical |

A small occlusion does not blind the shelf. A shopper's arm reaching in covers
a few percent and the reading stands; a shopper standing squarely in front
covers most of it and it does not.

`shelf_status` deliberately consults two different snapshots: the most recent
one in which the shelf was *visible* supplies the numbers, so a shopper does
not blank the display, while the most recent snapshot *of any kind* supplies
`is_occluded`, so the operator is told the view is currently blocked rather
than shown a stale figure with no caveat.

### UNKNOWN is not zero

A shelf reports `UNKNOWN` when it is occluded, when it has no camera, when
monitoring is off, or when it has never been read. None of these mean empty,
and the summary counts them separately.

Surfacing that count is a feature. "We cannot see 6 of your shelves" is
something the operator needs to know and can act on. Quietly folding those six
into "everything looks fine" is how a monitoring system becomes decorative.

## Avoiding phantom events

A detector that misses one box for one frame, a shadow, a reflection off
packaging: each briefly changes coverage. Emitting on every change would bury
one real stockout under hundreds of meaningless ones.

Three mechanisms, all in `InventoryEngine`:

**Interval sampling.** Shelves are read every `sample_interval_seconds`
(default 60), not per frame. Stock changes on a human timescale, and running
this every frame would burn CPU to recompute the same number.

**Stability.** A new value must repeat within `stability_tolerance` (0.05)
across `stability_samples` (2) consecutive readings before it is believed. One
anomalous reading changes nothing. If the value moves mid-confirmation the
sequence restarts rather than averaging across the transition.

**Cooldowns.** Each observation kind has its own per-shelf cooldown (120s), and
level events fire on *entry* to a level only. A shelf that sits empty all
afternoon is one stockout, not one every minute.

## The event vocabulary

| Observation | Means |
|-------------|-------|
| `PRODUCT_REMOVED` | Coverage fell by more than `change_threshold` |
| `PRODUCT_RETURNED` | Coverage rose by more than `change_threshold` |
| `SHELF_RESTOCKED` | Coverage rose by more than `restock_threshold` |
| `LOW_STOCK` | The shelf entered the LOW band |
| `SHELF_EMPTY` | The shelf entered the EMPTY band |
| `SHELF_CHANGED` | Something moved, but not enough to name |

Every one of those describes **the shelf**. None describes a person, and none
asserts a motive.

`PRODUCT_REMOVED` means coverage went down. It does not mean somebody took
something. Restocking staff rotating stock, a customer buying an item, a
display being tidied, a pallet parked in front of the camera and a detector
having a bad frame all produce the same observation, and the system says so.

Correlating a removal with the till is [milestone 16](POS_INTEGRATION.md). What
that produces is `POSSIBLE_INVENTORY_DISCREPANCY` — an inference, hedged in its
own name, carrying the observations it rests on so a human can audit and
disagree. Until milestone 16 exists, that event type cannot fire at all.

`nearby_track_keys` records which anonymous tracks were at *that* shelf when a
change was confirmed. It is computed per shelf, and it is context for a human
deciding whether to pull the footage — never an attribution. Presence at a
shelf is not evidence that this person moved the stock. See
[`PRIVACY.md`](PRIVACY.md).

## Configuring a shelf

| Field | Default | Notes |
|-------|---------|-------|
| `polygon` | whole frame | Normalised 0..1, so it survives a switch to a sub-stream. Must be ≥ 0.2% of the frame |
| `baseline_occupancy` | `null` | Coverage when known full. Uncalibrated shelves under-report |
| `low_threshold` | 0.35 | At or below this, stock is low enough to act on |
| `empty_threshold` | 0.12 | At or below this, the shelf reads empty |
| `sample_interval_seconds` | 60 | 5–3600 |
| `products` | none | Optional. Sets which detector classes count as stock |

Thresholds must stay ordered (`empty ≤ low`); reversed, every shelf would read
EMPTY forever, so the API rejects it.

Attaching products is optional. A shelf with no products measures *anything
that is not an occluder*, which is the right default for a generic model on an
unlabelled shelf — a box is a box. Attaching products whose
`detection_class_name` is set narrows measurement to those classes, and that
field is only populated once a model exists that can recognise them
([milestone 15](MILESTONES.md)).

Shelf geometry is validated at the API with the same functions the engine uses
at runtime. A shape the engine could not measure is rejected with an
explanation, rather than stored and then silently never read.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/products` | List the catalogue (`category`, `search`, `include_inactive`) |
| `POST` | `/products` | Add a product |
| `PATCH`/`DELETE` | `/products/{id}` | Update / soft-delete |
| `GET` | `/shelves` | List shelves (`store_id`, `camera_id`) |
| `POST` | `/shelves` | Configure a shelf |
| `PATCH`/`DELETE` | `/shelves/{id}` | Update / soft-delete |
| `GET` | `/inventory/status` | Every shelf's latest known state |
| `GET` | `/inventory/summary` | Counts for the overview |
| `GET` | `/inventory/snapshots` | Reading history |
| `GET` | `/inventory/events` | Confirmed changes |
| `POST` | `/shelves/{id}/snapshots` | A pipeline submitting a reading |

Deletes are soft throughout: the snapshots and events that reference a shelf,
and the events that reference a product, must still resolve afterwards.

Snapshot ingest accepts a `captured_at` in the past rather than stamping
arrival time. Edge pipelines post readings back when connectivity returns, and
an event must be attributable to when it happened.

## Retention

Snapshots follow `SNAPSHOT_RETENTION_DAYS` (default 30). Inventory events
follow `EVENT_RETENTION_DAYS` (default 90). See [`PRIVACY.md`](PRIVACY.md).
