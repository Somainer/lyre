# Seeds — worked coordination shapes

These are **examples of what the `coordination-imagination` procedure generates**,
NOT a menu to pick from. Each is a goal → shape (STEP 1) → primitives (STEP 2) →
the move. Read a few to calibrate what "deriving a shape" feels like, then derive
your own with STEP 1–5. If you find yourself copying one verbatim, stop and
re-run the procedure on YOUR actual goal — the value is the derivation, not the
catalog.

Format: **Name** — *base shape (modifier)* — primitives — what it buys.

---

## A. Resilience & supervision

- **Watchdog / dead-man's switch** — *loop (supervised)* — recurring self-mail
  with `max_occurrences` + checkpoint. Arm a heartbeat that re-checks a
  long-running condition; the *absence* of a fresh check is itself the alarm. If
  a wakeup dies, the next scheduled tick still fires — the switch is in the
  harness, not in memory.
- **OTP pager / supervised restart** — *barrier (supervised)* — `dispatch_task`
  legs + the reaper's `task_terminated` mail. Treat a leg's terminal-failure mail
  as a page: on receipt, decide re-plan / re-spec / fresh-agent / give up. Never
  re-dispatch the identical failing leg — it already proved it crashes.
- **Self-healing map-reduce** — *tree (supervised)* — `fan_in` over N shards +
  per-leg terminal mail. Map shards in parallel; when a shard fails, re-shard or
  retry just that shard, then reduce over whatever landed (`missing_legs` tells
  you what's absent). Partial results beat a stalled barrier.

## B. Decision & consensus

- **Quorum-as-tribunal (jury)** — *panel* — `fan_in` quorum=K, fresh
  `create_agent` per juror + `result_schema` with a verdict enum. N independent
  minds vote the SAME question; you tally + judge. Independence is the whole point
  — one mind agreeing with itself is not a jury.
- **Hung-jury escalation** — *panel (ratchet)* — jury → if no quorum verdict,
  escalate to a bigger/stronger panel or to the owner. Don't force a verdict from
  a split; surface the split as the signal.
- **Silent consensus via reactions** — *panel* — broadcast a proposal +
  `mailbox_react`. "Object within the window or it ships" — agreement costs zero
  wakeups (a react, not a send). Only dissent spends tokens.
- **Delphi method** — *panel (ratchet)* — round 1 independent estimates via
  `fan_in`; share the anonymized spread; round 2 re-estimate. Converges experts
  without anchoring them on the loudest voice.
- **Sealed-bid auction** — *panel* — open a `thread_id` channel, collect bids as
  typed `fan_in` results (hidden until the barrier resolves), then award. Use to
  allocate a scarce task to the agent with the best fit/estimate.
- **Blameless postmortem** — *panel* — after an incident, fan out "what failed
  and why" to the agents involved + one neutral analyst; synthesize causes, not
  culprits. Feeds the 固化 loop below.

## C. Racing & hedging

- **Hedged race** — *barrier* — `fan_in` quorum < N. Launch the same job several
  ways (different model, different approach); take the first K to finish, drop the
  stragglers. Trades tokens for latency + a robustness floor.
- **Escalation ratchet** — *loop (ratchet)* — try the cheap pass; only on failure
  spend the expensive one. A single grounded check before a full adversarial
  panel; a small model before the flagship. Spend effort only where the cheap pass
  couldn't settle it.

## D. Decomposition

- **Recursive divide-and-conquer** — *tree* — `fan_in` whose legs each open their
  OWN `fan_in`. Powerful but costly; keep depth shallow (prefer flat N legs). A
  leg that needs more digging usually does it *within its own wakeup*, not by
  spawning a sub-barrier.
- **Saga pipeline (with compensation)** — *pipeline* — staged `dispatch_task`
  where each stage records, in the checkpoint, how to UNDO it. If a late stage
  fails, walk the compensations backward. Durable multi-step work that can roll
  itself back across a crash.
- **Blackboard** — *loop* — a shared notes/facts file several agents read and
  append to, coordinated by mail. Specialists contribute opportunistically until
  the board is "solved." Good when the solution path isn't known up front.

## E. Time & long-running

- **Durable sleep / timer** — *single* — future-mail (`deliver_in`). "Do nothing
  until T, then act" — survives SIGKILL because the timer is a row, not an
  in-memory `sleep`. (This completes the task and re-wakes a fresh one; keeping
  the SAME task alive across the sleep would need `park`, which has no agent tool
  yet — see the note in `SKILL.md`.)
- **Slow-burn research relay** — *long-runner* — one goal, many wakeups, each
  pushing one increment and self-mailing the next. The relay baton is the
  checkpoint; no wakeup holds the whole problem in memory.
- **Memory-consolidation sleep cycle** — *loop (long-runner)* — a periodic
  recurring self-mail that re-reads recent transcripts/notes and distills durable
  facts into `memory/facts/`. A "sleep cycle" that turns hot experience into cold
  long-term memory.
- **Heartbeat / liveness beacon** — *loop* — recurring self-mail that emits a
  cheap "still alive + here's my state" each tick; a supervisor watches for the
  gap. The beacon's silence is the failure signal.

## F. Self-improvement

- **Self-improving 固化 A/B loop** — *loop (ratchet)* — run a task two ways
  (two skill variants), judge via a panel, promote the winner from
  `proposed/ → approved/`. The harness's skill lifecycle IS the A/B promotion
  mechanism.
- **Completeness critic** — *single, as a final stage* — after any fan-out, one
  agent asks "what's missing — a modality not searched, a claim unverified, a
  source unread?" Its findings become the next round's work. Stops "looked
  everywhere" from meaning "looked once."

---

Every one of these is just STEP 1 (a shape) + STEP 2 (its primitives) + the
kill-test. None required a new primitive. When your goal doesn't match any of
them — which is the normal case — that's not a gap in the catalog, it's the
procedure doing its job: derive the shape your goal actually needs.
