"""Lark/Feishu channel — owner mailbox surface over a Lark bot.

Owner-facing IM channel: text + image messages flow both ways between
the authorized Lark user and the Lyre mailbox. Default routing is
``cfg.bootstrap.dispatcher_id``; ``@<agent_id>`` prefix or
thread-continuity overrides. Outbound goes through the standard
``channel_publish`` outbox kind for kill-safe delivery.

Public API exposed at this level:
  * :class:`LarkChannel` — the :class:`lyre.integrations.ExternalChannel`
    implementation.
"""

from __future__ import annotations

from .channel import LarkChannel

__all__ = ["LarkChannel"]
