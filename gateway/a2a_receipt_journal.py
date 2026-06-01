"""Local-only A2A receipt journal sink.

This adapter gives staged A2A receiver/canary runs a concrete receipt sink before
any gated #agent-comms posting exists. It only appends compact receipt lines to a
local text file; callers own path selection and rotation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


class A2AReceiptJournal:
    """Append compact A2A receipt lines to a local text file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, line: str) -> None:
        text = str(line or "").replace("\r", " ").replace("\n", " ").strip()
        if not text:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")

    def extend(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.append(line)

    def read_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return [line.rstrip("\n") for line in self.path.read_text(encoding="utf-8").splitlines()]
