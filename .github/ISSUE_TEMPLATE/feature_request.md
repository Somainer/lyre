---
name: Feature request
about: Suggest a capability for Lyre
title: ''
labels: enhancement
assignees: ''
---

## What you want to do

<!-- A real scenario, not an abstract feature. "I want my leader to X
     so that Y" beats "Lyre should support feature Z". -->

## Why the current behavior is insufficient

<!-- What did you try? What did you reach for that wasn't there? -->

## A possible shape

<!-- Optional. If you have an idea for how this could look as a CLI
     command, persona convention, dashboard surface, etc., describe it.
     Don't worry about being right — this is a starting point for
     discussion. -->

## Architectural sanity check

Does this proposal respect Lyre's five core laws? (See
[docs/concepts.md](../docs/concepts.md#the-five-laws).) In particular:

- [ ] Doesn't bypass mailbox as the inter-agent communication primitive
- [ ] Doesn't introduce volatile cross-wakeup state (use mailbox / memory / checkpoint instead)
- [ ] Survives a process kill at any point
- [ ] Doesn't couple to a specific LLM provider

If you ticked any of these *no*, that's not necessarily a dealbreaker —
just say why the trade-off is worth it.
