"""Page memory — tracks page state changes to give the agent context."""

from __future__ import annotations


class PageMemory:
    """Tracks page state changes to give the agent context about what happened.

    Uses URL changes and visual diff ratio (from VisionPipeline) instead of
    element-based tracking. Simpler and more reliable.
    """

    def __init__(self):
        self._prev_url: str = ""
        self._prev_title: str = ""
        self._step = 0

    def update(self, url: str, title: str, diff_ratio: float = 1.0) -> str:
        """Record new state and return a context string describing what changed."""
        self._step += 1
        changes = []

        if url != self._prev_url:
            if not self._prev_url:
                changes.append(f"Loaded: {url}")
            else:
                changes.append(f"Page changed: {self._prev_url} -> {url}")
        elif diff_ratio < 0.3:
            # Silent — no URL change and diff is small. Don't claim the page
            # is "unchanged" since we only measured our own diff threshold,
            # not what the page actually did. Callers decide how to describe it.
            pass

        self._prev_url = url
        self._prev_title = title

        if not changes:
            return ""
        return "Page context: " + ". ".join(changes)
