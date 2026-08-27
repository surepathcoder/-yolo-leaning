# POS integration

Connecting a till to VisionGuard, and matching what the till recorded against
what the camera saw.

## What correlation answers

> Does a sale explain this shelf change?

Three answers, and the third matters as much as the other two.

| Status | Means |
|--------|-------|
| `MATCHED` | A sale plausibly accounts for the change |
| `UNMATCHED` | **No sale was found in the window** |
| `UNCERTAIN` | The evidence is ambiguous, or the till data was incomplete |

`UNMATCHED` is a statement about **the data**, not about a person. That
distinction is the whole reason this subsystem is written the way it is, and
losing it is the failure mode that would make VisionGuard harmful rather than
merely inaccurate.

In a real shop the ordinary causes of an unmatched change are far more common
than the alternative:

- staff restocking, facing up, or moving stock between displays
- a customer picking an item up and putting it back in another aisle
- an item scanned under a different SKU, or rung up at another till
- the POS clock and the camera clock disagreeing by a minute
- the till being offline for the window
- the detector simply being wrong about that frame

Every unmatched explanation names these. A test asserts that no outcome this
system produces contains the words *theft*, *steal*, *stolen*, *shoplift*,
*criminal*, *guilty*, *suspect*, *culprit* or *dishonest*.

## The rule that outranks the rest

**An incomplete window can never produce a discrepancy.**

If the till was offline, or the export was truncated, or the API paged out,
then "no sale was found" carries no information at all. Reported as
`UNMATCHED` it would turn an afternoon of ordinary trading into a list of
suspicions — and it would do so most severely for the customer whose POS
integration is least reliable, which is exactly backwards.

So coverage is part of the provider contract. `FetchResult` carries
`complete` and `reason` alongside the transactions, and an incomplete window
yields `UNCERTAIN` with no inference. It can still confirm a *match*: absence
of evidence is unusable, presence of it is not.

Two cases are treated the same way, deliberately:

- The provider reported incomplete coverage.
- The store has **never imported any POS data**. A store that has not
  connected its till is not a store with no sales. Without this, switching
  inventory monitoring on would immediately flood a new customer with
  unexplained changes.

## Providers

```python
class POSProvider(ABC):
    key: str

    def fetch(self, since: datetime, until: datetime) -> FetchResult: ...
    def health(self) -> tuple[bool, str]: ...
```

Narrow on purpose. Fetching sales in a time range is the only thing
correlation needs, and a wider interface would have to be implemented badly by
every till that cannot support it.

| Key | Class | For |
|-----|-------|-----|
| `mock` | `InMemoryPOSProvider` | Tests and demos; the reference implementation |
| `csv` | `CSVPOSProvider` | A till with no API, exporting nightly |

Configuration lives in `Store.settings["pos"]`:

```json
{ "provider": "csv", "options": { "path": "/data/pos/sales.csv" } }
```

### The CSV provider

One row per line item; rows sharing a transaction id form one sale. Column
names are matched loosely, because small shops export from inconsistent tools:

| Field | Accepted headers |
|-------|------------------|
| id | `external_id`, `id`, `transaction_id`, `receipt_id` |
| time | `transaction_at`, `datetime`, `timestamp`, `date`, `time` |
| product | `product_ref`, `sku`, `product_code`, `item_code`, `barcode` |
| quantity | `quantity`, `qty`, `units` |
| till | `terminal_ref`, `terminal`, `till`, `lane`, `register` |

Three parsing decisions are deliberate:

**A bad row is skipped, and the window is marked incomplete.** One malformed
line must not cost the whole night's sales — but the rows that were lost must
not silently become unexplained shelf changes either.

**A naive timestamp is rejected** unless `assume_tzinfo` is set. Correlation
runs on a two-minute window; a silent hour of drift would make every match
wrong in the same direction, which is worse than making none.

**A missing export raises** rather than returning zero sales, which would
invent a shop-wide discrepancy out of a wrong file path.

**An unrecognised status becomes `PENDING`**, not `COMPLETED`. Only completed
transactions count as sales — a void, a refund or a pending sale did not move
stock off the shelf, and letting one "explain" a change would hide a real
discrepancy.

## Scoring

Three signals, combined as a weighted mean:

| Signal | Weight | What it measures |
|--------|--------|------------------|
| `time` | 0.50 | How close the sale is to the observation |
| `product` | 0.35 | Did the sale include something this shelf stocks |
| `quantity` | 0.15 | Is the size of the change consistent with the quantity sold |

Plus a `terminal` tie-breaker worth 0.05, applied only when the shelf has a
known nearby till. Not a signal in its own right: shops rearrange tills and
the mapping goes stale.

**The window is asymmetric.** A sale happens *after* stock leaves the shelf —
the customer picks the item up, walks to the till, queues. So the search runs
`window_seconds` forward but only a quarter of that backward, and the time
score decays four times faster for a sale that preceded the change. A
symmetric window either misses the queue or invites matches from before the
item was touched.

**Quantity is weighted low on purpose.** Occupancy is a coverage ratio, so an
implied unit count is an estimate built on an estimate. It can support a
match; a test asserts it cannot carry one alone.

**An unavailable signal is dropped, not scored zero.** A shelf with no
catalogue mapping, or a sale with no line items, makes the product signal
unavailable — it is removed from both the numerator and the divisor. Scoring
it as a failure would push every shelf in an uncatalogued shop towards
"unexplained".

Thresholds: `>= 0.65` matches, `>= 0.35` is uncertain, below that is
unmatched. Every result stores its `score_breakdown`, so the number is
auditable rather than magic.

## When an inference is raised

A `POSSIBLE_INVENTORY_DISCREPANCY` event is created only when **all four**
hold:

1. The shelf change was a **removal**. Stock arriving is a delivery.
2. POS coverage for the window was **complete**.
3. Correlation found **no plausible sale**.
4. The observation's own confidence is **at least 0.5**. A shaky detection is
   not a foundation for a conclusion about a shop.

Even then it arrives hedged:

- `is_inference = True`, so nothing downstream can read it as a measurement
- the inventory event it rests on in `source_event_ids`
- `rule_name = "pos_correlation"`, and the correlation result id in metadata
- severity `LOW`
- a summary written to survive being read aloud in a dispute: what was
  observed, what was not found, that a human needs to check it, and the
  ordinary explanations

## Review, and measuring accuracy

Every result can be reviewed by a person. The verdict is about **the
correlation**, never about a person:

| Outcome | Means |
|---------|-------|
| `CORRECT` | The match, or the lack of one, was right |
| `INCORRECT` | The system matched the wrong thing |
| `EXPLAINED` | Unmatched, but the reviewer knows the ordinary reason |
| `UNCLEAR` | Not enough to judge |

`EXPLAINED` is counted separately from `INCORRECT`: "unmatched, and I know
why" is not the system being wrong.

`accuracy` is `correct / (correct + incorrect)`, and is **null** until a human
has judged at least one result. An unmeasured system must not report "0%
accurate" — nor "100%".

This loop is the only way the accuracy of this subsystem can ever be measured
rather than assumed, which is why it ships with the feature rather than after
it.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/pos/transactions` | Import sales |
| `GET` | `/pos/transactions` | List imported sales |
| `POST` | `/pos/correlate` | Correlate outstanding shelf removals |
| `GET` | `/pos/correlations` | Correlation history |
| `GET` | `/pos/correlations/{id}` | One result, with its reasoning |
| `POST` | `/pos/correlations/{id}/review` | Record a human verdict |
| `GET` | `/pos/accuracy` | How correlation is performing |
| `GET`/`PUT`/`DELETE` | `/pos/config/{store_id}` | Provider configuration |

Import is idempotent on `(provider, external_id)`. Re-running a nightly export
or replaying a webhook is safe, and skipping the duplicate is what stops one
sale from explaining a shelf change twice.

An unresolved SKU is normal, not an error. Shops sell things that are not in
the VisionGuard catalogue; the line is stored with `product_id = null` and its
`external_product_ref` kept so the mapping can be re-derived later.

## Permissions and privacy

`pos:read` starts at **ANALYST**, not VIEWER. A guard watching the camera feed
has no business reading the shop's takings. `pos:write` (import and review)
starts at STORE_MANAGER; `pos:configure` is ORGANIZATION_ADMIN only.

Transactions store an anonymous `cashier_ref`, never a cashier's name.
Correlation needs an identifier to group by; it does not need an identity. A
business that wants to map that reference to a person can do so in its own HR
system, deliberately.

`nearby_track_keys` from an inventory observation is **not an input to the
score** — a test asserts the score is identical with and without it. Presence
at a shelf is not evidence that a person moved the stock. See
[`PRIVACY.md`](PRIVACY.md).
