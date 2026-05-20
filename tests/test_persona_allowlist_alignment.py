"""Alignment between identity preamble and persona allowlists.

The identity preamble (built by `_build_identity_preamble`) tells every
agent to use specific tools — `mailbox_send`, `mailbox_read`,
`mailbox_get_message`, `mark_read`, `read_memory`, `list_agents`, …. If a
persona's `allowed_lyre_tools` is missing one, the model will follow the
preamble's advice, call the tool, and hit ToolError "not in allowlist".

That happened in the wild: dispatcher's allowlist was missing
`mailbox_get_message`, and a real wakeup spent 11 minutes trying to read
prior mail before silent-closing (see troubleshoot transcript
019e40c5-6da5-…). This test codifies the invariant so the next gap is
caught at CI time, not at user-facing failure time.
"""

from __future__ import annotations

import pytest

from lyre.personas.seed import _shipped_persona_files, load_persona_from_file

# Tools that the identity preamble *always* mentions to *every* persona
# that gets the preamble (i.e. has any allowed_lyre_tools at all — the
# owner persona has [] and is excluded since it isn't an LLM agent).
_PREAMBLE_CORE_TOOLS: frozenset[str] = frozenset(
    {
        "mailbox_send",        # "Reply to a sender → mailbox_send(...)"
        "mailbox_read",        # "mailbox_read() returns unread mail"
        "mailbox_get_message", # "To read a specific message's full body"
        "mark_read",           # "To dismiss FYI mail: mark_read(msg_id=N)"
        "read_memory",         # notes file: read_memory("facts/agent-<id>-notes.md")
        "list_agents",         # KNOWING THE TEAM section
    }
)


def _personas_with_tools():
    """All SHIPPED personas that actually use tools (excludes owner stub).
    This is a CI correctness check on what Lyre ships, not on a user dir."""
    out = []
    for path in _shipped_persona_files():
        p = load_persona_from_file(path)
        if p.allowed_lyre_tools:  # non-empty
            out.append(p)
    return out


@pytest.mark.parametrize("persona", _personas_with_tools(), ids=lambda p: p.name)
def test_persona_allowlist_covers_preamble_core_tools(persona) -> None:
    """Every persona with any LLM tools must include the core mail +
    introspection set the preamble teaches them about."""
    allowed = set(persona.allowed_lyre_tools)
    missing = _PREAMBLE_CORE_TOOLS - allowed
    assert not missing, (
        f"persona {persona.name!r} is missing preamble-required tools: "
        f"{sorted(missing)}. Identity preamble tells the model to call "
        f"these; without them the model hits ToolError 'not in allowlist'. "
        f"Either add the tools or rewrite the preamble to be parametric."
    )
