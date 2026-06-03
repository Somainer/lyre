"""T3: a wakeup on a 主线 gets that thread's recent mail injected.

Stage 2's payoff: a stateless model juggling several main-lines won't reliably
pull the RIGHT thread's mail. So when the running task carries metadata.thread_id,
assemble_initial_user_message surfaces that thread's back-and-forth (this agent's
view), scoped to mail the agent actually sent or received.
"""

from __future__ import annotations

import pytest

from lyre.persistence.models import MailboxMessage, Task
from lyre.persistence.sqlite_impl import SqliteRepositories
from lyre.runtime.context import assemble_initial_user_message


def _task_on(thread_id: str | None) -> Task:
    return Task(
        id="t", persona_name="dispatcher", goal="g", acceptance="a",
        status="in_progress",
        metadata=({"thread_id": thread_id} if thread_id else None),
    )


def _text(msg) -> str:  # noqa: ANN001
    return msg.content[0].text


async def _mail(repos: SqliteRepositories, **kw) -> None:  # noqa: ANN003
    await repos.mailbox.insert_message(MailboxMessage(urgency="normal", body="...", **kw))


@pytest.mark.asyncio
async def test_thread_mail_is_injected_both_directions(repos: SqliteRepositories) -> None:
    for box in ("dispatcher", "owner"):
        await repos.mailbox.ensure_mailbox(box)
    await _mail(
        repos, recipient="dispatcher", external_id="m1", sender="owner",
        title="kickoff the migration", metadata={"thread_id": "T1"},
    )
    await _mail(
        repos, recipient="owner", external_id="m2", sender="dispatcher",
        title="dispatched to worker", metadata={"thread_id": "T1"},
    )
    msg = await assemble_initial_user_message(
        _task_on("T1"), mailbox_repo=repos.mailbox, agent_id="dispatcher"
    )
    text = _text(msg)
    assert "本主线" in text and "T1" in text
    assert "kickoff the migration" in text  # received (←)
    assert "dispatched to worker" in text   # sent (→)


@pytest.mark.asyncio
async def test_thread_mail_scoped_to_participant(repos: SqliteRepositories) -> None:
    for box in ("dispatcher", "owner", "worker", "reviewer"):
        await repos.mailbox.ensure_mailbox(box)
    await _mail(
        repos, recipient="dispatcher", external_id="a", sender="owner",
        title="MINE on thread", metadata={"thread_id": "T2"},
    )
    # Same thread, but a worker↔reviewer exchange the dispatcher isn't party to.
    await _mail(
        repos, recipient="reviewer", external_id="b", sender="worker",
        title="NOT MINE", metadata={"thread_id": "T2"},
    )
    text = _text(await assemble_initial_user_message(
        _task_on("T2"), mailbox_repo=repos.mailbox, agent_id="dispatcher"
    ))
    assert "MINE on thread" in text
    assert "NOT MINE" not in text


@pytest.mark.asyncio
async def test_no_thread_section_when_task_has_no_thread(repos: SqliteRepositories) -> None:
    text = _text(await assemble_initial_user_message(
        _task_on(None), mailbox_repo=repos.mailbox, agent_id="dispatcher"
    ))
    assert "本主线" not in text
