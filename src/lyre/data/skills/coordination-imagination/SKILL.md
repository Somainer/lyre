---
name: coordination-imagination
description: A generative procedure for turning ANY goal bigger than one wakeup — or needing more than one agent — into a coordination structure DERIVED from Lyre's primitives, instead of reaching for the pattern you used last time. Load it before designing a dispatch / fan-out / loop / long-run; it teaches you to invent the right shape (barrier, panel, pipeline, ratchet, watchdog, …) from two invariants and a primitive→affordance ladder, then kill-test it. Use when a task spans multiple steps/agents/wakeups, when you feel a pull to busy-wait, or when the real question is which coordination shape fits.
scope: global
---

# Coordination imagination (derive the structure, don't reach for a recipe)

A procedure for turning a goal that spans multiple **steps, agents, or wakeups**
into a coordination structure — *derived* from Lyre's primitives, not copied from
a fixed menu. Run it the moment a task is bigger than one wakeup or needs more
than one agent, **before** you reach for `dispatch_task` / `fan_in_open` / a
recurring self-mail. It's how you invent the shape that fits instead of
defaulting to the shape you used last time.

This is not a pattern list. The patterns in `seeds.md` are *output* of this
procedure. The procedure is the capability — internalize it and you generate
your own.

## The two invariants (internalize these; the rest is consequence)

**1. NO-BLOCKING TRANSFORM.** Lyre has no blocking await. So every "wait" you
imagine must become three concrete things:
- (a) emit a **durable row** — a dispatched task, an open `fan_in` barrier, a
  scheduled self-mail, a parked task;
- (b) **STOP** calling tools so the wakeup ends;
- (c) **name the wake-event** that brings you back — which mail, which
  barrier-resolve, which scheduled delivery, which resume.

If you can't name the wake-event, you haven't finished the design — you've
designed a hang.

**2. HARNESS-ENFORCES-THE-BOUND.** Every loop or fan-out must answer: *what makes
this stop, and WHO counts it?* The bound is never your good intentions in a
prompt — it's a harness-enforced number: a `fan_in` quorum/deadline, a recurring
self-mail's `max_occurrences`/`recur_until`, the dead-loop guard, a budget in
your checkpoint you check **before** re-arming. Name the counter and the
stop-condition, or you've designed a runaway.

## STEP 1 — shape-triage (name the shape before you build it)

Classify the goal into ONE base shape:

- **single** — one wakeup settles it. (Most tasks. Don't over-build.)
- **pipeline** — stages, each feeds the next. (research → spec → implement → review)
- **barrier** — N independent legs, synthesize when enough land. (`fan_in`)
- **panel** — N legs deciding the SAME question; you judge. (adversarial-review, juries)
- **loop** — repeat until a condition. (poll, accumulate-to-target, heartbeat)
- **long-runner** — one goal across many wakeups, self-driven. (hand to `long-runner`)

Then add modifiers: **recursive** (a leg is itself a barrier — keep depth
shallow, prefer flat), **ratchet** (escalate effort only when the cheap pass
fails), **supervised** (a watcher restarts/escalates failed legs), **tree**
(divide → conquer → merge upward).

Say the shape out loud. Half of all bad designs are a panel forced into a
pipeline, or a busy-wait where a barrier belonged.

## STEP 2 — the primitive → affordance ladder

Every primitive has a **latent** affordance beyond its obvious use. Read the
right-hand column and ask "which latent power does my shape need?":

| Primitive | Obvious use | Latent affordance |
|---|---|---|
| recurring self-mail | remind me later | harness-enforced **bounded loop** + **dead-man's switch** |
| `fan_in` quorum < N | wait for all | **hedged race** — take the first K, drop stragglers |
| `fan_in` result_schema | collect replies | **machine-checkable contract** — typed, validated on send |
| `mailbox_react` | acknowledge | **zero-wakeup signal** — silent consensus, no token burn |
| future-mail (`deliver_in`) | schedule | **durable timer** that survives SIGKILL |
| broadcast | announce | **one-to-many fan-out** without N sends |
| `thread_id` | group mail | a **conversation / auction channel** between agents |
| checkpoint | save state | a **budget ledger** + a **no-progress detector** across wakeups |
| `create_agent` per leg | parallelism | **independent minds** — real disagreement, not one mind nodding |

The imagination move is reading a latent affordance and realizing your shape is
*already expressible*. You rarely need a new primitive — you need to see the one
you have differently.

> **A latent primitive you can't yet call.** Lyre's substrate has `park`/`resume`
> (a task can go dormant in `needs_input` and be revived later) — but there is
> **no agent tool** for it, and parking makes the agent **deaf to mail** (it
> drops out of Phase 0 auto-wake), so it would only be safe with a *guaranteed
> scheduler-driven* resume like a deadline — never a wait-for-mail or
> wait-for-approval. If a shape genuinely needs to keep a task *alive but paused*,
> that's a **gap to escalate**, not a tool to reach for. Seeing latent substrate
> capability — and naming the gap honestly — is itself the imagination move.

## STEP 3 — goal → roles

Turn the shape into concrete agents. Who argues which side? Who is read-only
(`analyst`) vs runs code (`worker`)? Who judges? Independence is a feature: a
fresh `create_agent` per leg buys real disagreement; reusing one agent buys
continuity. Choose on purpose.

## STEP 4 — mailbox is the only bus

Every coordination edge you drew collapses to a mailbox primitive: dispatch =
task-mail, wait = barrier/park + a named wake-event, signal = `react`, announce =
broadcast, converse = `thread`. If you imagined an edge that ISN'T a mailbox
primitive, it's a sidechannel — redesign it (law 5).

## STEP 5 — kill-test audit (not done until it passes)

Walk the design; at every "wait" ask: *if SIGKILL hits right now, does the system
recover?* The durable row from invariant 1 is what makes the answer yes. If
killing mid-wait loses work or hangs forever, an in-memory wait is hiding
somewhere — return to invariant 1 and make it a row + a named wake-event.

## The four trigger-questions (ask before you reach for a tool)

1. Am I about to **wait in-memory** for something? → make it a durable row + a named wake-event.
2. This loop — **who counts it down, and what stops it?** → name the harness-enforced bound.
3. Does this decision deserve an **independent adversary** before I commit? → panel / adversarial-review.
4. Can "wait for an external thing" become a **row + a wake-event** instead of a poll? → future-mail (complete + self-mail). (Keeping the SAME task *alive* across the wait would need `park` — which has no agent tool yet; escalate it.)

## Worked examples (calibrate, don't copy)

This skill ships a companion **`seeds.md`** in the same directory as this file
(same path, filename swapped). It holds ~20 grounded patterns — watchdog
dead-man's switch, hedged race, escalation ratchet, recursive divide-and-conquer,
self-healing map-reduce, silent consensus, saga pipeline, sealed-bid auction,
Delphi, blameless postmortem, memory-consolidation sleep cycle, … — each mapped
to the primitives above. They are **examples of what this procedure generates**,
not a menu to pick from. Read a few to calibrate, then derive your own shape with
STEP 1–5. Load it with `read_memory("<this skill dir>/seeds.md")` — the absolute
path is this file's location with `SKILL.md` replaced by `seeds.md`.
