"""Data models (Pydantic) for Lyre persistent entities.

Maps 1:1 with PERSISTENCE_SCHEMA.md §3 tables.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

TaskStatus = Literal[
    "pending", "in_progress", "needs_input", "completed", "failed", "cancelled"
]
PersonaStatus = Literal["proposed", "approved", "deprecated"]
SkillStatus = Literal["proposed", "approved", "deprecated"]
AgentStatus = Literal["idle", "busy", "archived"]
Urgency = Literal["blocker", "high", "normal", "low"]
OutboxKind = Literal["mailbox_send", "tier1_notification"]


class Agent(BaseModel):
    """A running instance of a persona — orthogonal to the persona itself.

    Persona is the role template (one md file). Agent is "this specific
    instance, with its own mailbox, own task queue, possibly own model
    override." Multiple agents can share one persona; mail and tasks are
    addressed to agent_id, never to persona name.

    Spawned agents use `<persona>/<name>` ids (e.g.
    `worker-maintainer/refactor-auth`). Bootstrap agents (`owner`,
    `dispatcher`, `analyst-1`, `reviewer-1`, or their custom equivalents)
    keep bare ids. `parent_agent_id` records the spawner so a child can
    escalate / reply up the chain without searching.
    """

    id: str
    persona_name: str
    status: AgentStatus = "idle"
    # The agent that spawned this one. NULL for bootstrap agents
    # (`owner`, `dispatcher`, `analyst-1`, `reviewer-1`, or custom names).
    # String "owner" when the human created the agent directly via
    # CLI/dashboard. Otherwise an existing agent_id.
    parent_agent_id: str | None = None
    created_at: datetime | None = None
    archived_at: datetime | None = None
    # JSON metadata. Currently recognized keys:
    #   model_id     — override the persona's model_preference with this single
    #                  model id (router still falls back to persona prefs if
    #                  it's unhealthy/unavailable)
    #   description  — freeform "this agent is for X"
    metadata: dict[str, Any] | None = None

    @property
    def model_id(self) -> str | None:
        return (self.metadata or {}).get("model_id")

    @property
    def description(self) -> str | None:
        return (self.metadata or {}).get("description")


class Persona(BaseModel):
    name: str
    role_description: str
    system_prompt: str
    allowed_lyre_tools: list[str] = Field(default_factory=list)
    # Persona declares model preference (tier + requires + ranked prefer list),
    # not a specific model identity. The router resolves against
    # model_registry.yaml at wakeup time.
    model_preference: dict[str, Any] | None = None
    needs_worktree: bool = True
    status: PersonaStatus = "approved"
    proposed_by_task_id: str | None = None
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Task(BaseModel):
    id: str
    parent_task_id: str | None = None
    # agent_id is canonical (one running instance). persona_name is kept as a
    # denormalized convenience (filled from agent.persona_name on insert) so
    # existing scheduler / router code that keys off persona name keeps
    # working until a follow-up cleanup migration drops it.
    agent_id: str | None = None
    persona_name: str
    goal: str
    acceptance: str
    status: TaskStatus
    lease_duration_s: int = 1800
    lease_holder: str | None = None
    lease_until: datetime | None = None
    checkpoint: dict[str, Any] | None = None
    tier_overrides: dict[str, Any] | None = None
    deadline: datetime | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None


class TaskSpec(BaseModel):
    """Input for creating a new task.

    Either `agent_id` or `persona_name` must be supplied. If only `agent_id`
    is given, the repository fills `persona_name` from the agent record. If
    only `persona_name` is given (back-compat for early-adopter call sites
    that haven't migrated), the repository errors — callers must dispatch
    to a real agent.
    """

    agent_id: str | None = None
    persona_name: str | None = None
    goal: str
    acceptance: str
    parent_task_id: str | None = None
    lease_duration_s: int = 1800
    tier_overrides: dict[str, Any] | None = None
    deadline: datetime | None = None
    metadata: dict[str, Any] | None = None


class Wakeup(BaseModel):
    id: str
    task_id: str
    agent_id: str | None = None
    persona_name: str
    started_at: datetime
    ended_at: datetime | None = None
    end_status: str | None = None
    token_input: int | None = None
    token_output: int | None = None
    wall_clock_ms: int | None = None
    tool_call_count: int | None = None
    provider: str | None = None
    model: str | None = None
    failure_report: dict[str, Any] | None = None
    transcript_uri: str | None = None
    # Per-wakeup context metrics (migration 0006). `context_peak_tokens`
    # is the max input_tokens any single turn reported — proxy for
    # "how close to the model's context window did we get". `compaction_
    # count` is how many times the wakeup auto-compacted mid-flight.
    context_peak_tokens: int | None = None
    compaction_count: int = 0


ScheduledMailStatus = Literal["pending", "completed", "cancelled", "bounced"]
RecurKind = Literal["interval", "cron"]


class ScheduledMail(BaseModel):
    """A mailbox_send scheduled for a future moment, optionally recurring.

    See migrations/0004_scheduled_mail.sql for lifecycle and recurrence
    semantics.
    """

    id: int | None = None
    recipient: str
    sender: str
    urgency: Urgency = "normal"
    title: str | None = None
    body: str
    task_id: str | None = None
    parent_msg_id: int | None = None
    metadata: dict[str, Any] | None = None

    scheduled_for: datetime

    recur_kind: RecurKind | None = None
    recur_value: str | None = None
    recur_until: datetime | None = None
    occurrence_count: int = 0

    created_at: datetime | None = None
    created_by_agent: str | None = None
    created_by_task: str | None = None
    status: ScheduledMailStatus = "pending"
    last_delivery_id: int | None = None
    last_delivered_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancelled_by: str | None = None
    bounce_reason: str | None = None


class MailboxMessage(BaseModel):
    id: int | None = None
    recipient: str
    external_id: str
    sender: str
    urgency: Urgency
    # `title` is the inbox-listing subject line (≤140 char). Auto-derived
    # from body's first non-empty line if the sender didn't supply one.
    # Set at write time so listings stay deterministic and cache-friendly.
    title: str | None = None
    body: str
    task_id: str | None = None
    parent_msg_id: int | None = None
    # Broadcast support (0002): when an agent sends to N recipients, each
    # delivered row shares a broadcast_id and lists everyone on the thread
    # in recipients_all (so any reader can reply-all without round-tripping).
    broadcast_id: str | None = None
    recipients_all: list[str] | None = None
    metadata: dict[str, Any] | None = None
    delivered_at: datetime | None = None
    # Per-message read state (0005). NULL = unread. Auto-set by
    # `mailbox_read` when the agent sees the row, or explicitly by
    # `mark_read`. Owner-side mailbox stays NULL (owner is a human).
    read_at: datetime | None = None


class OutboxRow(BaseModel):
    id: int | None = None
    task_id: str
    wakeup_id: str
    kind: OutboxKind
    payload: dict[str, Any]
    external_id: str
    created_at: datetime | None = None
    dispatched_at: datetime | None = None
    dispatch_attempts: int = 0
    last_error: str | None = None


class Skill(BaseModel):
    id: str
    name: str
    frontmatter: dict[str, Any]
    body: str
    status: SkillStatus
    source_task_id: str | None = None
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    scope: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Artifact(BaseModel):
    id: str
    task_id: str
    wakeup_id: str
    kind: str
    content_hash: str
    blob_uri: str
    size_bytes: int | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None
