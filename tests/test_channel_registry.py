"""Tests for the ExternalChannel Protocol + ChannelRegistry.

These exercise only the abstract framework — channel implementations
(Lark, future Slack, …) get their own test files. The point is to
verify the seam itself is sound: any class with the right four
members satisfies the Protocol, registration enforces uniqueness,
and lookup is O(1).
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from lyre.integrations import ChannelRegistry, ExternalChannel
from lyre.persistence.models import MailboxMessage


class _FakeChannel:
    """Minimal class that satisfies ExternalChannel via duck typing.
    Doesn't inherit anything — Protocol satisfaction is structural."""

    name: ClassVar[str] = "fake"

    def __init__(self) -> None:
        self.published: list[tuple[MailboxMessage, str | None]] = []
        self.run_called = False

    async def run(self, stop_event: asyncio.Event) -> None:
        self.run_called = True
        await stop_event.wait()

    async def publish_owner_mail(
        self,
        msg: MailboxMessage,
        reply_to_external_id: str | None,
    ) -> str | None:
        self.published.append((msg, reply_to_external_id))
        return f"fake-{msg.id}"


def test_fake_channel_satisfies_protocol() -> None:
    """Structural Protocol: a class with the right ClassVar and
    methods passes isinstance(...) check at runtime."""
    ch: ExternalChannel = _FakeChannel()  # type-checker satisfaction
    assert ch.name == "fake"


def test_registry_register_and_get() -> None:
    reg = ChannelRegistry()
    ch = _FakeChannel()
    reg.register(ch)
    assert reg.get("fake") is ch
    assert reg.get("missing") is None


def test_registry_rejects_duplicate_name() -> None:
    reg = ChannelRegistry()
    reg.register(_FakeChannel())
    import pytest
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_FakeChannel())


def test_registry_iteration_helpers() -> None:
    reg = ChannelRegistry()
    assert not reg  # empty registry is falsy → easy "if registry:" gate
    assert len(reg) == 0
    assert reg.names() == []

    reg.register(_FakeChannel())
    assert reg  # non-empty is truthy
    assert len(reg) == 1
    assert reg.names() == ["fake"]
    assert len(reg.values()) == 1
