# Privacy

VisionGuard watches people at work and while shopping. That is a serious thing
to build, and the design reflects it.

## Principles

### 1. Anonymous by default

The system tracks **objects**, not identities.

```
Person, track 42, camera 4          ← what VisionGuard records
Juma Mwangi entered the warehouse   ← what VisionGuard does not record
```

A track id is meaningful only within one camera and one pipeline session. It is
deliberately *not* stable across cameras or restarts: `track_key` includes a
session id precisely so yesterday's track 42 and today's track 42 cannot be
conflated into a person's history.

### 2. No facial recognition

Face recognition is **not implemented and not enabled by default**. Detection
answers "is there a person here", not "which person". Adding face recognition
would require a deliberate, documented decision with a lawful basis, subject
consent where required, and its own retention policy — not a configuration flag.

Adding it casually would change what the product is.

### 3. Observations, not accusations

The vision engine reports what it can measure. It never asserts intent.

| Recorded | Never recorded |
|----------|----------------|
| `PERSON_ENTERED_ZONE` | "Intruder" |
| `PRODUCT_REMOVED`, no matching POS transaction | "Theft" |
| `LOITERING`, 84 s in zone 3 | "Suspicious person" |

The event vocabulary is enforced by a test: no event type may contain `THEFT`,
`STEAL`, `SHOPLIFT`, `CRIMINAL` or `GUILTY`. Inference-level events must be
hedged (`POSSIBLE_`, `POTENTIAL_`) and must record the observations they rest
on so a human can audit and disagree.

An unmatched shelf change is not theft. Restocking, staff movement, a
mis-scanned item, a POS outage and a false detection all produce the same
signal. The system says so.

### 4. Staff are people too

POS transactions store an anonymous `cashier_ref`, not a cashier's name.
Correlation needs an identifier, not an identity. A business that wants to map
that reference to a person can do so in its own HR system, deliberately —
VisionGuard does not do it for them by default.

### 5. Minimise what leaves the premises

On the edge topology, raw video **never leaves the customer's site**. Only
event metadata and explicitly configured evidence synchronise to the cloud.
This is a privacy control as much as a bandwidth one.

## What is stored

| Data | Contains | Default retention |
|------|----------|-------------------|
| Detections | Class, confidence, bounding box, timestamp | 90 days |
| Tracks | Anonymous id, trajectory, first/last seen | 90 days |
| Events | Type, severity, zone, track id, summary | `EVENT_RETENTION_DAYS` (90) |
| Snapshots | A still frame that may show a person | `SNAPSHOT_RETENTION_DAYS` (30) |
| Clips | Short video that may show a person | `EVIDENCE_RETENTION_DAYS` (30) |
| Audit log | Who did what in the system | Long-lived by design |

Detections are **sampled, not exhaustive**: storing every detection of every
frame would be roughly 26 million rows per camera per month and would create a
far more invasive record than the product needs.

Retention is configurable per deployment, and evidence rows carry an explicit
`expires_at` so deletion is scheduled rather than aspirational.

## Access control

- Evidence (snapshots and clips) requires the `evidence:view` permission.
- **`ANALYST` deliberately cannot view evidence.** Analysing traffic patterns
  and peak hours does not require looking at pictures of people. This is the
  clearest example of least privilege in the role design.
- Every evidence access is written to the audit log.
- Cross-tenant access by a platform operator is logged against the customer's
  organization, visible in their own audit view.

## Obligations for deployers

VisionGuard is a tool. Deploying it lawfully is the operator's responsibility.
In most jurisdictions, including Tanzania and the wider EAC, that means at
minimum:

1. **Signage.** Visible notice that CCTV with analytics is in use, at every
   entrance and in monitored areas.
2. **A lawful basis.** Usually legitimate interest in security and stock
   control. Document it.
3. **Staff notification.** Employees must be told what is monitored, what is
   recorded and for how long — before deployment, not after an incident.
4. **Proportionate placement.** Never in toilets, changing rooms, prayer rooms
   or staff rest areas. Zones should cover what needs protecting, not
   everything the lens can see.
5. **Retention discipline.** Keep footage only as long as there is a reason to.
   The defaults are a starting point, not a legal opinion.
6. **Subject access.** Be able to answer a request about what is held on an
   individual. Anonymous tracking makes this genuinely difficult to answer
   precisely — which is a feature, but it must be explained honestly rather
   than used to stonewall.
7. **A human decides.** Never take disciplinary or legal action on an
   `is_inference` event alone. It is a prompt to review evidence, not a finding.

## What this system will not do

- Identify individuals by face or gait.
- Score or rank employees on productivity.
- Infer demographics — age, gender, ethnicity — from video.
- Claim that a person committed a crime.
- Export a person's movement history across a chain of stores.

If a future requirement pushes against one of these, it needs an explicit
decision recorded here, not a quiet configuration change.

## Data subject requests

Because tracking is anonymous, VisionGuard usually **cannot** link stored data
to a named person, and therefore cannot produce "everything you hold about me"
in the way a CRM can. Deployers should say this plainly.

Where an individual can identify a specific incident (a time, a place, an
event), the operator can locate the relevant events and evidence and act on a
deletion request through the retention tooling.
