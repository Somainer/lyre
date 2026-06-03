"""T2: thread_id (主线) propagates mechanically through mail + tasks.

The runtime carries the thread, not the (amnesiac) agent: a reply inherits the
parent's thread, a dispatched child inherits the dispatching wakeup's thread,
and mail sent during a wakeup inherits that wakeup's thread. Explicit always
wins. Owner-side minting lives in `lyre send`.
"""

from __future__ import annotations

from typing import Any

import pytest

from lyre.persistence.models import MailboxMessage, Persona, TaskSpec
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.tools import ToolContext
from lyre.runtime.tools.mailbox import MAILBOX_SEND
from lyre.runtime.tools.tasks import DISPATCH_TASK


async def _seed_agent(repos: SqliteRepositories, name: str) -> None:
    await repos.personas.upsert(
        Persona(name=name, role_description=name, system_prompt=name)
    )
    await repos.agents.create(agent_id=name, persona_name=name)


async def _ctx(
    repos: SqliteRepositories, *, thread_id: str | None, agent_id: str = "dispatcher",
) -> ToolContext:
    """A context backed by a real task + open wakeup (outbox FKs wakeup_id)."""
    task_id = await repos.tasks.create(
        TaskSpec(agent_id=agent_id, goal="g", acceptance="a")
    )
    wakeup_id = await repos.wakeups.start(task_id, agent_id, agent_id=agent_id)
    await repos.tasks.claim_lease(task_id, wakeup_id, duration_sec=600)
    return ToolContext(
        repos=repos, task_id=task_id, wakeup_id=wakeup_id,
        persona_name=agent_id, agent_id=agent_id, thread_id=thread_id,
    )


async def _sent_metadata(repos: SqliteRepositories) -> dict[str, Any]:
    batch = await repos.outbox.dequeue_batch(limit=10)
    assert batch, "expected an outbox row"
    return batch[0].payload.get("metadata") or {}


@pytest.mark.asyncio
async def test_mail_inherits_the_wakeups_thread(repos: SqliteRepositories) -> None:
    await _seed_agent(repos, "dispatcher")
    await repos.mailbox.ensure_mailbox("owner")
    ctx = await _ctx(repos, thread_id="thread-abc")
    await MAILBOX_SEND.handler(ctx, {"to": "owner", "body": "hi", "_tool_use_id": "tu1"})
    assert (await _sent_metadata(repos))["thread_id"] == "thread-abc"


@pytest.mark.asyncio
async def test_reply_inherits_parents_thread(repos: SqliteRepositories) -> None:
    await _seed_agent(repos, "dispatcher")
    await repos.mailbox.ensure_mailbox("dispatcher")
    parent_id = await repos.mailbox.insert_message(
        MailboxMessage(
            recipient="dispatcher", external_id="p1", sender="owner",
            urgency="normal", body="...", metadata={"thread_id": "thread-parent"},
        )
    )
    # The replying agent is on no thread of its own — it must pick up the
    # parent's, not leave the reply orphaned.
    ctx = await _ctx(repos, thread_id=None)
    await MAILBOX_SEND.handler(
        ctx, {"to": "owner", "body": "re", "reply_to": parent_id, "_tool_use_id": "tu2"},
    )
    assert (await _sent_metadata(repos))["thread_id"] == "thread-parent"


@pytest.mark.asyncio
async def test_explicit_thread_id_wins(repos: SqliteRepositories) -> None:
    await _seed_agent(repos, "dispatcher")
    await repos.mailbox.ensure_mailbox("owner")
    ctx = await _ctx(repos, thread_id="ctx-thread")
    await MAILBOX_SEND.handler(
        ctx, {"to": "owner", "body": "x", "thread_id": "explicit", "_tool_use_id": "tu3"},
    )
    assert (await _sent_metadata(repos))["thread_id"] == "explicit"


@pytest.mark.asyncio
async def test_no_thread_means_no_thread_metadata(repos: SqliteRepositories) -> None:
    # Not everything is on a thread; absent everywhere → no thread_id stamped.
    await _seed_agent(repos, "dispatcher")
    await repos.mailbox.ensure_mailbox("owner")
    ctx = await _ctx(repos, thread_id=None)
    await MAILBOX_SEND.handler(ctx, {"to": "owner", "body": "x", "_tool_use_id": "tu4"})
    assert "thread_id" not in (await _sent_metadata(repos))


@pytest.mark.asyncio
async def test_dispatch_child_inherits_dispatching_thread(repos: SqliteRepositories) -> None:
    await _seed_agent(repos, "dispatcher")
    await _seed_agent(repos, "analyst")
    ctx = await _ctx(repos, thread_id="thread-xyz", agent_id="dispatcher")
    res = await DISPATCH_TASK.handler(
        ctx, {"agent": "analyst", "goal": "g", "acceptance": "a"},
    )
    child = await repos.tasks.get(res["task_id"])
    assert child is not None
    assert child.metadata is not None and child.metadata["thread_id"] == "thread-xyz"
