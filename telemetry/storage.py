"""JSONL trace loading, validation, and indexing."""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .schema import ValidationError, validate_trace_events


@dataclass
class Trace:
    path: Path | None
    events: list[dict[str, Any]]

    @property
    def header(self) -> dict[str, Any]:
        for event in self.events:
            if event.get("type") == "trace_start":
                return event.get("data", {})
        return {}

    @property
    def trace_id(self) -> str | None:
        return self.header.get("trace_id")


@dataclass
class TraceIndex:
    by_type: dict[str, list[dict[str, Any]]]
    by_seq: dict[int, dict[str, Any]]


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def load_trace(path: str | Path) -> Trace:
    p = Path(path)
    events: list[dict[str, Any]] = []
    with _open_text(p) as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{p}:{line_no}: invalid JSON: {exc}") from exc
    return Trace(path=p, events=events)


def write_jsonl(path: str | Path, events: Iterable[dict[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="\n") as fh:
        for event in events:
            fh.write(json.dumps(event, separators=(",", ":"), sort_keys=True))
            fh.write("\n")


def validate_trace(trace: Trace) -> list[ValidationError]:
    return validate_trace_events(trace.events)


def index_events(trace: Trace) -> TraceIndex:
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_seq: dict[int, dict[str, Any]] = {}
    for event in trace.events:
        by_type[event.get("type", "")].append(event)
        seq = event.get("seq")
        if isinstance(seq, int):
            by_seq[seq] = event
    return TraceIndex(by_type=dict(by_type), by_seq=by_seq)


def dump_json(path: str | Path, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

