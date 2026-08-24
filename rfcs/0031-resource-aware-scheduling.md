# RFC 0031: Resource-Aware Scheduling for the Swarm

**Status**: Draft

## Summary
The swarm's tick is time-gated (every 20 minutes) and lane-gated (one worker per lane), but
resource-blind. Both failure directions have already happened: on 2026-07-07 zed went dark from
too many concurrent browser processes with no memory cap (pagekite 503, suspected OOM), and on
2026-07-08 the operator reported keeping the swarm busy for 45 minutes after going to bed —
followed by nine idle hours. Make contended resources first-class: **declare** them in a
registry, **lease** the exclusive ones, **pace** the budgeted ones, and let the tick consult
both before spawning.

## Resource model
Two kinds, needing different mechanics:

- **Mutex resources** — at most N holders. The 3090 on fractal (jamlab's swarm contends on it),
  browser slots on zed (the 07-07 incident: envoy-browser + browser-box + gating agent + e2e rig
  concurrently), RAM headroom. Five tasks that are otherwise parallelizable serialize here: only
  one of five may hold the GPU at a time, and that must be a scheduling fact, not a crash.
- **Budget resources** — a quantity that refills on a schedule. The z.ai/GLM subscription's
  5-hour and weekly quotas, provider API concurrency limits. Nothing breaks at the limit
  (requests queue or fail politely), but burning the whole 5-hour window in one parallel burst
  is strictly worse than incremental work across the window.

## Mechanics
**Registry.** A `resources.json` next to `tick.sh`: name, kind, capacity, and for budgets a
window ("gpu: mutex 1", "glm: budget, window 5h + weekly"). No daemon.

**Leases for mutexes.** A lane or issue declares needs (a `needs:gpu` label on the issue, or a
per-lane default in the tick). Before spawning, the tick takes `flock` on the resource's lock
file; no lock, no spawn, the issue stays queued. Same precedent as the e2e-bridge flock fix.
The worker holds the lock for its lifetime (flock on the spawned process, so a dead worker
releases implicitly — no reaper).

**Pacing for budgets.** A spend ledger per budget resource (append tokens-used per worker run;
provider usage endpoints where they exist). The tick computes remaining/window-remaining and
scales concurrency down as the ratio drops — full lanes when flush, one incremental worker when
tight, zero when exhausted. The 20-minute tick is already the right cadence for this; the change
is that the tick reads the ledger instead of only counting `ready` issues.

**Two-sided.** The same ledger that caps bursts should notice slack: budget flush + queue empty
is the nine-idle-hours state. That combination can trigger a low-priority discretionary lane
(maintenance sweeps, exploration, self-directed work) — and it is the natural consumer of
RFC 0010's ambient intake, which exists to keep the queue from being empty in the first place.

## Relation to existing work
- **RFC 0028** is this pattern for one resource: the browser pool is lease-per-request +
  metering + reset. This RFC generalizes lease+meter to all contended resources; the 0028 pool
  becomes one named entry in the registry rather than a special case.
- **td-0023** — the staging CVM's autonomous loop is the main spender; its lanes are the ones
  the ledger throttles.
- The 07-07 zed incident is the mutex case study; the unspent-subscription complaint
  ("GPU maxing, where I can't use more than a tiny bit of my subscription") is the budget one.

## v1 (deliberately small)
- `resources.json` + lock-file directory.
- ~30 lines in `tick.sh`: flock acquisition for declared needs; a budget check that skips or
  halves spawning under a threshold.
- One ledger append in the worker wrapper.
No scheduler process, no priorities beyond the existing p1/p2/p3, no cross-host coordination —
fractal and zed each run their own registry. Cross-host (sharing one GPU ledger between two
swarms) is a later RFC if it's ever real.
