from __future__ import annotations

import statistics
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class LatencySample:
    name: str
    elapsed_ms: float


@dataclass
class LatencyRecorder:
    samples: list[LatencySample] = field(default_factory=list)

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        start = time.perf_counter_ns()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
            self.samples.append(LatencySample(name=name, elapsed_ms=elapsed_ms))

    def summary(self) -> dict[str, float]:
        if not self.samples:
            return {"count": 0, "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0}

        values = [sample.elapsed_ms for sample in self.samples]
        sorted_values = sorted(values)
        p95_index = min(len(sorted_values) - 1, int(len(sorted_values) * 0.95))

        return {
            "count": float(len(values)),
            "avg_ms": statistics.fmean(values),
            "p50_ms": statistics.median(values),
            "p95_ms": sorted_values[p95_index],
        }

    def by_stage(self) -> dict[str, float]:
        return {sample.name: sample.elapsed_ms for sample in self.samples}
