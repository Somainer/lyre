---
name: adversarial-review
description: Run a contested or high-stakes decision/review as an adversarial debate — fan out a prosecution + a defense, each evidence-grounded, then judge. Use for irreversible or expensive calls, "should we build/merge/ship X", or any review prone to single-perspective bias or self-rationalization.
scope: global
---

# Adversarial review (debate-then-judge)

A decision procedure for **contested or high-stakes** calls. Instead of deciding
from one perspective (which rationalizes), fan out independent agents with
**assigned opposing stances**, make each **ground its case in evidence**, then
**judge** by weighing both. It is how you stop a plausible-but-wrong conclusion —
including your own — from surviving.

## When to use it (gate on cost — this spends N wakeups)

Use it ONLY when the decision is one or more of:
- **the owner explicitly asks for it** — they ask for an adversarial review /
  a red-team / a debate, or name this skill (then run it even for a lower-stakes
  call — the owner asked),
- **irreversible / expensive** (merge, ship, delete, a costly architecture choice),
- **contested** (reasonable people disagree; you feel a pull to rationalize),
- **a review** of work where being wrong is costly, or
- a claim you're tempted to accept because it's convenient.

Do **NOT** use it for routine work, low-stakes calls, or anything a single
grounded check settles — a debate is several wakeups of tokens; spend them only
where single-perspective bias is the real risk.

## The recipe (compose dispatch_task + fan_in)

1. **Frame** the exact question and the **decision bar**: what concretely makes
   the answer YES vs NO. Write it down — both sides and the judge anchor on it.
2. **Open the barrier**: `fan_in_open(expect_replies=N, quorum=N, result_schema=<the schema below>)`.
3. **Spin up the debaters — fresh, independent agents (the independence IS the
   point).** For EACH leg, `create_agent(persona="analyst")` then `dispatch_task`
   it into the group. **Persona = `analyst`** (read-only: it gathers evidence +
   argues) — use **`worker`** instead only if a leg must RUN code/tests to make
   its case. A new agent per leg = real disagreement, not one mind agreeing with
   itself. Give genuinely **opposing** stances: ≥1 **PROSECUTION** (argue FOR)
   and ≥1 **DEFENSE** (argue AGAINST); a second angle per side for a hard call.
   Each leg's `goal` must say:
   *"You are the {prosecution|defense}. Argue your side as strongly as you can,
   GROUNDED in evidence (read the code/data; cite file:line or the source).
   Default to conceding honestly: fill `concedes` with what you give up. Do your
   own digging WITHIN this wakeup (read_memory / shell / python). Do NOT
   dispatch sub-agents — you are a leaf; the debate stays one level."*
4. **Each leg submits a typed result** (the schema below) — not a plain mail.
   A leg that finishes without submitting is failed-loud and surfaced as a
   failed leg, so the debate can't silently lose a side. **The legs do NOT fan
   out** — keep the debate flat (N sibling legs + the judge); a leg that needs
   more digging does it inside its own wakeup, not by spawning children.
5. **You are the judge.** When the barrier resolves, the coordinator that ran
   this skill (**you** — `dispatcher` / `long-runner`, NOT a separately
   dispatched agent) is auto-woken; in THAT wakeup read `fan_in_results`, weigh
   both sides, and produce a verdict that **reflects BOTH** and states **"what
   evidence would flip it."** Default to the **skeptical** side unless the case
   is genuinely made. Do not just tally — name the decisive piece of evidence.

## Non-negotiables (drop any one and it's theater, not a debate)

Independent agents (one per leg) · assigned FOR vs AGAINST · a mandatory
`concedes` field · every claim evidence-cited (file:line / data) · a separate
judge that defaults skeptical and states what would flip the call.

## Leg result schema (typed — the fan-in barrier's contract)

```json
{
  "stance": "prosecution | defense",
  "strongest_case": "the single strongest, specific argument",
  "evidence": "file:line / data / source backing it — concrete, not abstract",
  "concedes": "what you honestly grant the other side"
}
```

**Your verdict to the owner is structured TEXT — a `mailbox_send`, NOT JSON.**
The owner reads prose, not a payload. Send these labelled lines:

> **Verdict:** <the ruling>
> **Confidence:** low / medium / high
> **Decisive evidence:** <the one fact that settles it — file:line / source>
> **What would flip it:** <the concrete observation/evidence that changes the call>
> **Failed/missing legs:** <any leg that failed or didn't submit, + why>

(Only the LEGS' results are typed JSON — that's the barrier's machine contract,
read by you the judge via `fan_in_results`. The owner-facing verdict is prose.)

## Caveats (read before invoking)

- **Cost**: a debate is several wakeups. High-stakes only.
- **Two sycophantic legs = theater.** The value is real adversariality + real
  evidence. If a side can't ground its case, that itself is signal.
- **Bias toward the status quo / the cheaper action** in the judge unless the
  case to change is genuinely made — adding complexity should clear a bar.

## Worked shape (abstract)

> Question: "Should we merge change X?" Bar: merge only if it fixes a real,
> reachable problem with no existing mitigation. → fan out PROSECUTION ("X fixes
> a real gap, here's the reachable path + why existing guards miss it") and
> DEFENSE ("X is over-design — the problem is already bounded / unobserved").
> Each reads the code and cites it. Judge: weigh both; if the prosecution can't
> show a reachable unguarded path, **don't merge** — and record what observation
> would justify revisiting. (This procedure has killed plausible changes by
> surfacing, from the `concedes` fields, that the change was both unneeded *and*
> technically wrong.)
