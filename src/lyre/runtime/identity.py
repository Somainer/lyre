"""Agent identity — single source of truth for the agent_id format.

Format:
  - Bootstrap agents: bare lowercase token (`owner`, `leader`).
  - Spawned agents:   `<persona>/<name>` where each segment is
    lowercase letters/digits/hyphens, persona must start with a letter,
    name must start with a letter or digit.

The regex is enforced by `create_agent` (the only spawn path that
takes external input) and by `mailbox_send` recipient validation. We
deliberately do NOT enforce it on `agents.create` itself so the
bootstrap seed can keep its bare-id agents.

Format guarantees:
  * `splitId(id)` is unambiguous — at most one `/` per id.
  * No collisions between persona names and spawn names (persona
    can't contain `/`, names can't either).
  * Filesystem-safe (matches the `agent-<id>-notes.md` file convention,
    where `/` becomes a one-level directory).
"""

from __future__ import annotations

import re

# Two anchored patterns: the bootstrap form (bare) and the spawned form.
# Persona segment: starts with a letter, then letters/digits/hyphens.
# Name segment: starts with a letter or digit, then letters/digits/hyphens.
_PERSONA_RE = r"[a-z][a-z0-9-]*"
_NAME_RE = r"[a-z0-9][a-z0-9-]*"
AGENT_ID_RE = re.compile(rf"^{_PERSONA_RE}(/{_NAME_RE})?$")

BOOTSTRAP_IDS: frozenset[str] = frozenset({"owner", "leader"})


def is_valid_agent_id(agent_id: str) -> bool:
    """True iff `agent_id` matches the agent-id grammar."""
    return bool(AGENT_ID_RE.fullmatch(agent_id))


def is_bootstrap(agent_id: str) -> bool:
    """`owner` and `leader` are special: bare ids, no parent, can't be
    spawned via `create_agent`. Everything else must use persona/name."""
    return agent_id in BOOTSTRAP_IDS


def split_id(agent_id: str) -> tuple[str, str | None]:
    """`worker-maintainer/refactor-auth` → (`worker-maintainer`,
    `refactor-auth`). Bare ids return (id, None)."""
    if "/" in agent_id:
        persona, name = agent_id.split("/", 1)
        return persona, name
    return agent_id, None


def compose_id(persona: str, name: str | None) -> str:
    """Inverse of split_id. If name is None, returns just the persona
    (caller is responsible for ensuring that's a bootstrap id)."""
    if name is None or name == "":
        return persona
    return f"{persona}/{name}"


def validate_agent_id(agent_id: str) -> None:
    """Raise ValueError if `agent_id` doesn't match the grammar.

    Use at trust boundaries: the `create_agent` tool, the `mailbox_send`
    recipient validator, the dashboard send form. Anti-hallucination
    measure: the model invented agent ids like `leader-scheduler` before
    we added validation.
    """
    if not is_valid_agent_id(agent_id):
        raise ValueError(
            f"invalid agent_id {agent_id!r}: must be either a bootstrap "
            f"id (`owner`, `leader`) or `persona/name` with each "
            f"segment matching {_PERSONA_RE!r}."
        )
