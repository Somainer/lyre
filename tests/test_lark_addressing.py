"""Pure-function tests for the Lark addressing parser."""

from __future__ import annotations

from lyre.integrations.lark.addressing import resolve


def test_default_recipient_when_no_prefix_or_thread() -> None:
    """The most common path: bare message → dispatcher. The body
    survives unchanged except for leading whitespace trim."""
    r = resolve(
        "  hello team, what's up?  ",
        default_recipient="dispatcher",
    )
    assert r.recipient == "dispatcher"
    assert r.body == "hello team, what's up?  "
    assert r.source == "default"


def test_explicit_prefix_overrides_default() -> None:
    """`@worker-maintainer/refactor-auth body` addresses that
    specific agent and strips the prefix from the body."""
    r = resolve(
        "@worker-maintainer/refactor-auth ship the PR plz",
        default_recipient="dispatcher",
    )
    assert r.recipient == "worker-maintainer/refactor-auth"
    assert r.body == "ship the PR plz"
    assert r.source == "explicit_prefix"


def test_explicit_prefix_strips_following_punctuation() -> None:
    """`@dispatcher: body` and `@dispatcher，body` (CJK comma) should
    both produce body="body" — drop one separator-ish character
    after the prefix for readability."""
    assert resolve(
        "@dispatcher: report status", default_recipient="x",
    ).body == "report status"
    assert resolve(
        "@dispatcher，看下这个", default_recipient="x",
    ).body == "看下这个"
    assert resolve(
        "@dispatcher\n\nmulti line", default_recipient="x",
    ).body == "multi line"


def test_thread_recipient_wins_over_default() -> None:
    """Thread reply → inherit the original mail's recipient. Body
    untouched (no prefix to strip)."""
    r = resolve(
        "got it, on the way",
        default_recipient="dispatcher",
        thread_recipient="worker-maintainer/coco-skills",
    )
    assert r.recipient == "worker-maintainer/coco-skills"
    assert r.body == "got it, on the way"
    assert r.source == "thread"


def test_thread_wins_even_with_prefix() -> None:
    """Inside an existing thread, `@<agent>` prefix is NOT
    interpreted as a redirect — keeps the thread coherent. If the
    user wants to talk to someone else they start a new thread.
    The prefix text rides along in the body as-is."""
    r = resolve(
        "@analyst meanwhile, fyi",
        default_recipient="dispatcher",
        thread_recipient="worker-maintainer/refactor-auth",
    )
    assert r.recipient == "worker-maintainer/refactor-auth"
    # Body is left alone (with whitespace lstrip).
    assert r.body.startswith("@analyst")
    assert r.source == "thread"


def test_prefix_must_start_with_letter() -> None:
    """`@123-numbers` doesn't match — Lyre agent ids start with a
    letter. Falls through to default."""
    r = resolve(
        "@123 wat",
        default_recipient="dispatcher",
    )
    assert r.recipient == "dispatcher"
    assert r.source == "default"


def test_bare_persona_without_name_works() -> None:
    """`@dispatcher` (no `/name`) is a valid agent_id — the bootstrap
    singletons use this shape."""
    r = resolve(
        "@dispatcher just a ping",
        default_recipient="some-fallback",
    )
    assert r.recipient == "dispatcher"
    assert r.body == "just a ping"


def test_prefix_in_middle_of_body_doesnt_match() -> None:
    """The prefix only triggers when it's at the START of the
    message. `lookup @worker-foo manually` is just body text."""
    r = resolve(
        "lookup @worker-foo manually",
        default_recipient="dispatcher",
    )
    assert r.recipient == "dispatcher"
    assert r.source == "default"
    assert r.body == "lookup @worker-foo manually"
