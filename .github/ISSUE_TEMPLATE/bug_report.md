---
name: Bug report
about: Something behaved unexpectedly
title: ''
labels: bug
assignees: ''
---

## What you ran

<!-- The command(s) you executed, env vars you set, etc. -->

```bash
# example:
export DEEPSEEK_API_KEY=sk-...
uv run lyre serve
uv run lyre send leader "..."
```

## What happened

<!-- Full output / error. Paste the actual text rather than describing it. -->

## What you expected

<!-- What should have happened instead. -->

## Diagnostic info

<details><summary>lyre wakeups list --since 30m</summary>

```
<paste output here>
```

</details>

<details><summary>Relevant transcript snippet (if applicable)</summary>

```
# uv run lyre audit --latest --persona <X> --json | jq 'select(.type=="tool_use")'
<paste output here>
```

</details>

## Environment

- OS:
- Python version (`uv run python --version`):
- Lyre git SHA (`git rev-parse HEAD`):
- LLM provider used:
