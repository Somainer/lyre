"""Agent identity — single source of truth for the agent_id GRAMMAR.

Format:
  - Bare ids:     lowercase token, used by bootstrap-seeded agents whose
                  display_name owns the slot (owner; dispatcher / analyst-1
                  / reviewer-1 by default, or custom display names like
                  "luna" / "scribe" the owner picks in identity.md).
  - Spawned ids:  ``<persona>/<name>`` where each segment is lowercase
                  letters/digits/hyphens; persona starts with a letter,
                  name starts with a letter or digit.

The regex is enforced by ``create_agent`` (the only spawn path that
takes external input) and by ``mailbox_send`` recipient validation. We
deliberately do NOT enforce it on ``agents.create`` itself so the
bootstrap seed can keep its bare-id agents.

Format guarantees:
  * ``split_id(id)`` is unambiguous — at most one ``/`` per id.
  * No collisions between persona names and spawn names (persona can't
    contain ``/``, names can't either).
  * Filesystem-safe via ``flat_id()``: ``/`` is flattened to ``-`` so every
    agent's notes/scratchpad live as ONE flat file (never a directory
    layer). ``agent_notes_rel_path()`` below is the single source of truth
    for the notes filename — runtime writers and the identity preamble must
    agree on it byte-for-byte or an agent's long-term memory silently forks.

This module is the id grammar AND its filesystem mapping. It deliberately
does NOT export "is this id a bootstrap-seeded singleton?" — that's
runtime state (live in ``agents.parent_agent_id IS NULL`` in the DB), not
a syntactic property. Callers needing that distinction query the agent
table directly.
"""

from __future__ import annotations

import re

# Two anchored patterns: the bare form and the spawned form.
# Persona segment: starts with a letter, then letters/digits/hyphens.
# Name segment: starts with a letter or digit, then letters/digits/hyphens.
_PERSONA_RE = r"[a-z][a-z0-9-]*"
_NAME_RE = r"[a-z0-9][a-z0-9-]*"
AGENT_ID_RE = re.compile(rf"^{_PERSONA_RE}(/{_NAME_RE})?$")


def is_valid_agent_id(agent_id: str) -> bool:
    """True iff ``agent_id`` matches the agent-id grammar."""
    return bool(AGENT_ID_RE.fullmatch(agent_id))


def is_bare_id(agent_id: str) -> bool:
    """True iff ``agent_id`` has no ``/`` — i.e. is in the bare form used
    by bootstrap-seeded agents. NOT a check on whether the agent IS
    bootstrap (that's DB state via ``parent_agent_id IS NULL``)."""
    return "/" not in agent_id


def split_id(agent_id: str) -> tuple[str, str | None]:
    """``worker-maintainer/refactor-auth`` → (``worker-maintainer``,
    ``refactor-auth``). Bare ids return (id, None)."""
    if "/" in agent_id:
        persona, name = agent_id.split("/", 1)
        return persona, name
    return agent_id, None


def compose_id(persona: str, name: str | None) -> str:
    """Inverse of split_id. If name is None, returns just the persona
    (caller is responsible for ensuring that's a valid bare id)."""
    if name is None or name == "":
        return persona
    return f"{persona}/{name}"


def flat_id(agent_id: str) -> str:
    """Filesystem-flat form of an agent id: ``worker-maintainer/backend-1``
    → ``worker-maintainer-backend-1``. Every per-agent file (notes,
    scratchpad, notes archive) is named with this form so spawned ids never
    create a directory layer. Centralised here because four inline copies
    plus one site that forgot the flatten already produced a real bug
    (spawned agents' auto-summaries written to a path nobody reads)."""
    return agent_id.replace("/", "-")


def agent_notes_rel_path(agent_id: str) -> str:
    """Path of the agent's long-term notes file relative to memory_root.

    Single source of truth shared by seed (file creation), context (the
    identity preamble that tells the agent where its notebook lives), and
    wakeup_summary (the runtime appender/rotator)."""
    return f"facts/agent-{flat_id(agent_id)}-notes.md"


def validate_agent_id(agent_id: str) -> None:
    """Raise ValueError if ``agent_id`` doesn't match the grammar.

    Use at trust boundaries: the ``create_agent`` tool, the
    ``mailbox_send`` recipient validator, the dashboard send form. Anti-
    hallucination measure: models invent agent ids like
    ``leader-scheduler`` from time to time.
    """
    if not is_valid_agent_id(agent_id):
        raise ValueError(
            f"invalid agent_id {agent_id!r}: must be a bare lowercase "
            f"token or ``persona/name`` with each segment matching "
            f"{_PERSONA_RE!r}."
        )
