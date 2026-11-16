"""Lightweight pipeline profiler."""

import sys
import time


class Timer:
    """Tracks timing for pipeline stages."""

    def __init__(self, name: str = "pipeline"):
        self.name = name
        self._start = time.perf_counter()
        self._marks: list[tuple[str, float]] = []

    def mark(self, label: str):
        """Record a checkpoint."""
        self._marks.append((label, time.perf_counter()))

    def summary(self) -> str:
        """Formatted multi-line summary."""
        lines = [f"[{self.name}]"]
        prev = self._start
        for label, t in self._marks:
            lines.append(f"  {label}: {(t - prev) * 1000:.0f}ms")
            prev = t
        if self._marks:
            total = (self._marks[-1][1] - self._start) * 1000
            lines.append(f"  TOTAL: {total:.0f}ms")
        return "\n".join(lines)

    def oneline(self) -> str:
        """Compact single-line summary for SSE events."""
        parts = []
        prev = self._start
        for label, t in self._marks:
            parts.append(f"{label}={int((t - prev) * 1000)}ms")
            prev = t
        if self._marks:
            total = int((self._marks[-1][1] - self._start) * 1000)
            parts.append(f"total={total}ms")
        return " | ".join(parts)

    def deltas(self) -> dict[str, int]:
        """Return {label: delta_ms} dict, including _total."""
        result = {}
        prev = self._start
        for label, t in self._marks:
            result[label] = int((t - prev) * 1000)
            prev = t
        if self._marks:
            result["_total"] = int((self._marks[-1][1] - self._start) * 1000)
        return result

    def log(self):
        """Print summary to stderr."""
        print(self.summary(), file=sys.stderr)
