"""Lyre tool registry — in-process for Sprint 1.

The agent loop builds tool input_schema from these declarations and dispatches
tool_use blocks to the right handler. Same surface will be exposed via the MCP
server later (Sprint 2+); each handler is provider-neutral on purpose.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ...adapter.llm_adapter import LyreToolSpec
from ...persistence.repositories import Repositories


@dataclass
class ToolContext:
    """Per-wakeup context every tool handler receives."""

    repos: Repositories
    task_id: str
    wakeup_id: str
    persona_name: str
    # `agent_id` is the canonical identity for mailbox addressing and
    # dispatch. `persona_name` stays as a denormalized convenience for
    # router / prompt code. Default None for back-compat with older
    # test fixtures that haven't migrated; new code paths always populate
    # both. Tools that absolutely need agent_id (e.g. mailbox defaults)
    # fall back to persona_name when this is None.
    agent_id: str | None = None
    # Extra slots added later (worktree dir, ssh sock, etc.)
    extras: dict[str, Any] = field(default_factory=dict)
    # Captured end_wakeup(...) call args. Populated by the END_WAKEUP
    # handler when the agent declares wakeup termination; the agent
    # loop reads this after each tool dispatch to decide whether the
    # wakeup is over. None means no terminal declaration yet — see
    # docs/design/WAKEUP_END_CONTRACT.md.
    end_wakeup_declaration: dict[str, Any] | None = None

    @property
    def self_mailbox(self) -> str:
        """The mailbox key that mail to me lands in. agent_id when set;
        falls back to persona_name for legacy callsites."""
        return self.agent_id or self.persona_name


class ToolError(Exception):
    """Raised by a tool handler to signal a structured error to the LLM.

    The agent loop catches this, wraps the message as a tool_result with
    is_error=True, and continues — i.e. lets the LLM observe and recover.
    """


ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def to_spec(self) -> LyreToolSpec:
        return LyreToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
        )


class ToolRegistry:
    """Holds Tool objects keyed by name; filters by persona's allowlist."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs_for(self, allowed_names: list[str]) -> list[LyreToolSpec]:
        """Return tool specs filtered by an allowlist.

        Empty allowlist → empty result (defense-in-depth: a persona with no
        allowed_lyre_tools sees no tools at all).
        """
        return [
            self._tools[n].to_spec() for n in allowed_names if n in self._tools
        ]

    def all_names(self) -> list[str]:
        return list(self._tools.keys())
