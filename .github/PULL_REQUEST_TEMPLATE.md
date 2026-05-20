## Summary

<!-- One or two sentences: what changed and why. -->

## Scope

<!-- Files/subsystems touched. Helps reviewers know where to look. -->

## Testing

<!-- How you verified the change. Real commands you ran, not "tested locally". -->

```bash
uv run pytest -q
uv run ruff check
# any other commands / scenarios you exercised
```

## Architecture check

If your change touches any of the five core laws (mailbox-only inter-agent
comms, kill-test recoverability, three persistence tiers, provider
neutrality, Lyre tool gateway), say so here and explain why your approach
respects them. If not, "n/a" is fine.

## Migration / data change

- [ ] No schema change
- [ ] Schema change with new migration under `migrations/` (numbered next ordinal)

## Docs

- [ ] No user-visible change
- [ ] Updated relevant doc(s) under `docs/`
- [ ] Updated relevant internal design doc at the repo root
