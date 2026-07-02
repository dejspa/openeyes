"""Page memory — tracks page state changes to give the agent context."""

from __future__ import annotations


class PageMemory:
    """Tracks page state changes to give the agent context about what happened.

    One instance per session; URL history is kept per tab (keyed by an opaque
    tab key) so tab switches don't produce bogus "Page changed" messages.
    """

    def __init__(self):
        self._prev_url: dict[int, str] = {}  # tab_key -> last seen URL

    def update(self, tab_key: int, url: str, diff_ratio: float = 1.0) -> str:
        """Record new state and return a context string describing what changed."""
        prev = self._prev_url.get(tab_key, "")
        self._prev_url[tab_key] = url

        if url != prev:
            if not prev:
                return f"Page context: Loaded: {url}"
            return f"Page context: Page changed: {prev} -> {url}"
        # No URL change — stay silent. We only measured our own visual diff,
        # not what the page actually did, so don't claim "unchanged".
        return ""

    def forget(self, tab_key: int) -> None:
        self._prev_url.pop(tab_key, None)

    def prune(self, live_keys: set[int]) -> None:
        for key in list(self._prev_url):
            if key not in live_keys:
                del self._prev_url[key]
