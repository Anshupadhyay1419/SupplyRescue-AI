"""
Cancellation token for tool calls that may time out.

Problem: Python cannot safely kill a running thread. When a tool times out,
the agent stops WAITING for it, but the worker thread keeps running and could
still save data to the database later - after the agent already told the LLM
"this tool failed".

Solution: the agent and the tool share one CancellationToken.
  - The tool saves its changes ONLY through `commit_if_not_cancelled()`.
  - When the agent gives up, it calls `cancel()`.
A lock makes these two steps mutually exclusive, so exactly one of them wins:
  * cancel first  -> the tool's transaction is rolled back (nothing saved), or
  * commit first  -> the changes were saved BEFORE the timeout; the agent is
                     told so and can look at the current data.
Either way, no database change can appear "late".
"""

import threading


class ToolCancelledError(Exception):
    """Raised inside a tool when it tries to save after the agent cancelled it."""


class CancellationToken:
    def __init__(self):
        self._lock = threading.Lock()
        self._cancelled = False
        self._committed = False

    def cancel(self) -> bool:
        """
        Called by the agent on timeout.

        Returns True if the tool can no longer save anything,
        False if the tool had already committed its changes.
        """
        with self._lock:
            if self._committed:
                return False
            self._cancelled = True
            return True

    def commit_if_not_cancelled(self, connection) -> None:
        """Called by the tool's transaction instead of connection.commit()."""
        with self._lock:
            if self._cancelled:
                raise ToolCancelledError("Tool was cancelled after a timeout; changes discarded.")
            connection.commit()
            self._committed = True
