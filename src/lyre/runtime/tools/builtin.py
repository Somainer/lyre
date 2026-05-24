"""Builds the default Lyre ToolRegistry containing every built-in tool."""

from __future__ import annotations

from . import ToolRegistry
from .introspect import (
    ARCHIVE_AGENT,
    CREATE_AGENT,
    LIST_AGENTS,
    LIST_MODELS,
    LIST_PERSONAS,
    LIST_TASKS,
    READ_MEMORY,
    UPDATE_SCRATCHPAD,
)
from .mailbox import (
    CANCEL_SCHEDULED_MAIL,
    LIST_SCHEDULED_MAIL,
    MAILBOX_GET_MESSAGE,
    MAILBOX_REACT,
    MAILBOX_READ,
    MAILBOX_SEND,
    MARK_READ,
)
from .progress import END_WAKEUP, REPORT_SIDE_EFFECT
from .python import PYTHON_EXEC
from .shell import SHELL_EXEC
from .tasks import DISPATCH_TASK, QUERY_TASK_STATUS


def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool in (
        MAILBOX_SEND,
        MAILBOX_READ,
        MAILBOX_GET_MESSAGE,
        MAILBOX_REACT,
        MARK_READ,
        LIST_SCHEDULED_MAIL,
        CANCEL_SCHEDULED_MAIL,
        END_WAKEUP,
        REPORT_SIDE_EFFECT,
        DISPATCH_TASK,
        QUERY_TASK_STATUS,
        READ_MEMORY,
        UPDATE_SCRATCHPAD,
        LIST_PERSONAS,
        LIST_AGENTS,
        LIST_MODELS,
        LIST_TASKS,
        CREATE_AGENT,
        ARCHIVE_AGENT,
        PYTHON_EXEC,
        SHELL_EXEC,
    ):
        reg.register(tool)
    return reg
