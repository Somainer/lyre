"""Parse Lark-side message → Lyre mail recipient.

Lark messages from the authorized owner need to land in some agent's
inbox. Three resolution rules, in priority order:

  1. **Thread reply** — if the message is a reply inside an existing
     Lark thread that came from Lyre originally, the parent mail's
     ``metadata.channels.lark.message_id`` lets us look up which
     agent the original was addressed to. Same conversation continues
     with the same agent.

  2. **Explicit ``@<agent_id>`` prefix** — message body starts with
     ``@dispatcher`` or ``@worker-maintainer/refactor-auth``. Strip
     the prefix from the body and address the named agent. Lark's
     own @-mention markup (``<at user_id="..."></at>``) is for the
     bot account itself — separate concept; users address agents in
     body text, not via Lark mentions.

  3. **Default** — fall through to ``config.bootstrap.dispatcher_id``.
     Most natural "DM the team lead" semantics.

The parser is pure (no I/O), so it tests cheaply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Lyre agent id grammar: persona segment must start with a letter,
# then lowercase letters / digits / hyphens; optional /name segment
# follows the same rule. Matches what runtime.identity.is_valid_agent_id
# accepts. Used as the regex for the prefix parser so we don't have
# to invoke the identity module here (keeps the addressing parser
# dependency-free for tests).
_AGENT_ID_RE = re.compile(
    r"@([a-z][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)?)\b"
)


@dataclass(frozen=True)
class AddressingResult:
    """Outcome of resolving a Lark message to a mail recipient.

    ``body`` is the message text WITH any addressing prefix stripped
    — what the recipient agent actually reads. ``recipient`` is the
    resolved agent_id. ``source`` is a short tag for logging
    ("thread" / "explicit_prefix" / "default").
    """

    recipient: str
    body: str
    source: str


def resolve(
    message_body: str,
    *,
    default_recipient: str,
    thread_recipient: str | None = None,
) -> AddressingResult:
    """Choose the recipient agent_id and strip any addressing prefix
    from ``message_body``.

    Args:
        message_body: raw text from the Lark message (may begin with
            ``@<agent_id>`` and a separator).
        default_recipient: where to send when no other signal is
            present (typically ``config.bootstrap.dispatcher_id``).
        thread_recipient: if the message is a reply inside a thread
            we previously published to Lark, the agent_id of the
            original mail's recipient (from
            ``parent.metadata.channels.lark.recipient``). Pass None
            for non-threaded / first-message-in-thread.
    """
    # 1. Thread continuity wins. Even an `@<agent>` prefix inside a
    #    thread reply is treated as redirect rather than override —
    #    keep the address but allow body strip-only? No: the cleanest
    #    rule is "thread wins, prefix ignored when threaded", which
    #    avoids accidental fan-out. If the user really wants to
    #    redirect mid-thread they start a new thread.
    if thread_recipient:
        return AddressingResult(
            recipient=thread_recipient,
            body=message_body.lstrip(),
            source="thread",
        )

    # 2. Explicit `@<agent_id>` prefix.
    stripped = message_body.lstrip()
    match = _AGENT_ID_RE.match(stripped)
    if match:
        agent_id = match.group(1)
        # Body = everything after the prefix, with one leading
        # whitespace/colon/punctuation consumed for readability.
        rest = stripped[match.end():]
        rest = re.sub(r"^[\s:：，,。.]+", "", rest)
        return AddressingResult(
            recipient=agent_id,
            body=rest,
            source="explicit_prefix",
        )

    # 3. Default → dispatcher.
    return AddressingResult(
        recipient=default_recipient,
        body=message_body.lstrip(),
        source="default",
    )
