"""Back-compat shim — the class moved to mail_watcher.py and was renamed
MailWatcher (it now watches urgency≥high by default, not only blocker).
Existing imports continue to work via the aliases below."""

from .mail_watcher import (
    BlockerWatcher,
    MailWatcher,
    format_interrupt_notice,
    format_mail_notice,
)

__all__ = [
    "BlockerWatcher",
    "MailWatcher",
    "format_interrupt_notice",
    "format_mail_notice",
]
