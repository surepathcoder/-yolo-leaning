# Loss-prevention behaviour rules

Milestone 18 adds the first rule in a family the platform is built to grow:
generic behaviour signals that need **no per-shop training** — no custom
product model, no calibration, nothing a customer has to do beyond drawing
zones on their own camera. They work the moment a store configures them,
the same way `RESTRICTED_AREA_ENTRY` and `LOITERING` already do.

This matters for a multi-tenant product: milestone 15's custom models are
opt-in and per-organization. A signal that only works after a customer has
labelled their own dataset is not a baseline feature — it's an upsell. Loss
prevention has to work for a shop that signed up an hour ago.

## Possible exit without payment

`POSSIBLE_EXIT_WITHOUT_PAYMENT` fires when a person reaches a configured
**EXIT** zone without a recorded visit to a configured **CHECKOUT** zone on
the same camera, in the same tracking session.

Pure zone geometry. `ZoneType.EXIT` and `ZoneType.CHECKOUT` already existed
in the schema before this milestone — the dashboard's zone editor has always
let an installer draw them — but nothing consumed the distinction until now.

**Requires both zones configured, or it says nothing.** A store that has not
drawn a checkout zone has given the system no information about payment at
all; treating "we never asked" the same as "they skipped it" would flag
every exit at every store that hasn't finished setup. The rule checks for at
least one `CHECKOUT`-kind zone on the camera before it will ever fire —
matching the honesty rule `vision/pos/correlation.py` (milestone 16) applies
to a store with no POS data imported: absence of a signal is not evidence.

**What it refuses to conclude**

| Situation | Result |
|-----------|--------|
| No CHECKOUT zone configured on the camera | Silent — not evidence of anything |
| Checkout zone visited (fully passed through) before the exit | Silent |
| Non-person detection reaches the exit zone | Ignored |
| A person reaches the exit with no checkout visit this session | `POSSIBLE_EXIT_WITHOUT_PAYMENT`, hedged, `MEDIUM` severity, reviewed by a human |

Like every inference in this system, the event is hedged: `is_inference`
set, a confidence level attached, and the alert body states plainly that
this is "not a finding of wrongdoing" and needs a human to check. Paying at
a self-checkout the camera doesn't see as a "checkout zone," stepping out to
take a call and returning, a staff member on a break, and a genuine missed
payment all produce the same signal. See `docs/PRIVACY.md` §3 — the event
vocabulary itself is test-enforced to never claim otherwise.

**Known limits, accepted rather than solved here**

- **Same camera only.** A track's identity (`track_key`) is scoped to one
  camera's session; if checkout and exit are watched by different cameras,
  this rule cannot correlate them. Cross-camera re-identification does not
  exist anywhere in this codebase.
- **State resets on reconnect.** A brief camera drop right after a genuine
  checkout clears the rule's memory of that visit, same as `LoiteringRule`'s
  existing behaviour — an accepted class of limitation, not new here.
- **A pass-through, not a purchase check.** The rule only knows a track
  visited a zone shaped like a till; it has no idea whether a sale actually
  happened there. Milestone 16's POS correlation is the feature that knows
  about actual transactions — a future refinement could combine the two.

## What's next

Two more "different theft situations" were discussed and deliberately **not**
built in this milestone:

- **More zone/line correlation patterns** in this same no-training family —
  the natural next additions once this one has real-world signal.
- **A concealment-gesture signal** (hand moving toward a bag shortly after
  shelf contact) — requires pose estimation, which today's detection stack
  does not carry (`Detection`/`TrackedObject` are boxes-only), a real CPU
  performance budget check (a comparable per-frame cost was already rejected
  once for BoT-SORT re-identification — see `vision/tracking/bytetrack.py`),
  and a deliberate, documented privacy decision per `docs/PRIVACY.md` §2's
  own stated bar for biometric-adjacent capability. That's its own milestone
  with its own plan, not an extension of this one.
