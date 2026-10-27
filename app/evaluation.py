"""Measure a classifier model against a labelled set: accuracy per field, latency, tokens
and cost. See scripts/run_eval.py and evals/classification.jsonl."""

import json
import statistics
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.classifier import ClassificationError, classify
from app.llm.base import LLMProvider
from app.tally import SupportMessage

FIELDS = ("category", "urgency", "sentiment", "language", "order_id")

# USD per million tokens (input, output). Local models cost nothing per token.
# Check https://www.anthropic.com/pricing before relying on these.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


@dataclass
class Case:
    id: str
    message: str
    expected: dict[str, Any]
    form_order_id: str | None = None


def load_cases(path: str | Path) -> list[Case]:
    cases = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                cases.append(Case(**json.loads(line)))
    return cases


def _normalise(field_name: str, value: Any) -> Any:
    if field_name == "order_id" and isinstance(value, str):
        return value.strip().upper()
    return value


def price_for(model_name: str) -> tuple[float, float]:
    """Per-MTok prices for a provider name such as 'anthropic:claude-opus-5-5'."""
    _, _, model = model_name.partition(":")
    return PRICES_PER_MTOK.get(model or model_name, (0.0, 0.0))


@dataclass
class Report:
    provider: str
    total: int
    errors: list[dict[str, str]] = field(default_factory=list)
    correct: Counter = field(default_factory=Counter)
    category_confusion: Counter = field(default_factory=Counter)  # (expected, got) -> n
    mistakes: list[dict[str, Any]] = field(default_factory=list)
    latencies_ms: list[int] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def accuracy(self, field_name: str) -> float:
        return self.correct[field_name] / self.total if self.total else 0.0

    def as_dict(self) -> dict[str, Any]:
        lat = sorted(self.latencies_ms)
        answered = self.total - len(self.errors)
        return {
            "provider": self.provider,
            "cases": self.total,
            "failed": len(self.errors),
            # Every field is scored over all cases; a failed case counts as wrong.
            "accuracy": {f: round(self.accuracy(f), 3) for f in FIELDS},
            "exact_match": round(self.correct["all"] / self.total, 3) if self.total else 0.0,
            "latency_ms": {
                "p50": int(statistics.median(lat)) if lat else None,
                "p95": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
            },
            "tokens": {"input": self.input_tokens, "output": self.output_tokens},
            "cost_usd": {
                "total": round(self.cost_usd, 6),
                "per_1000_messages": round(self.cost_usd / answered * 1000, 4) if answered else None,
            },
            "category_confusion": [
                {"expected": e, "got": g, "count": n}
                for (e, g), n in sorted(self.category_confusion.items())
            ],
            "mistakes": self.mistakes,
            "errors": self.errors,
        }


def run_eval(provider: LLMProvider, cases: Iterable[Case]) -> Report:
    cases = list(cases)
    report = Report(provider=provider.name, total=len(cases))
    for case in cases:
        message = SupportMessage(message=case.message, order_id=case.form_order_id)
        started = time.perf_counter()
        try:
            outcome = classify(provider, message)
        except ClassificationError as exc:
            report.errors.append({"id": case.id, "error": str(exc)})
            continue
        report.latencies_ms.append(int((time.perf_counter() - started) * 1000))
        report.input_tokens += outcome.input_tokens or 0
        report.output_tokens += outcome.output_tokens or 0
        price_in, price_out = price_for(outcome.model)
        report.cost_usd += (
            (outcome.input_tokens or 0) * price_in + (outcome.output_tokens or 0) * price_out
        ) / 1_000_000

        got = outcome.result.model_dump()
        wrong = {}
        for f in FIELDS:
            if _normalise(f, got[f]) == _normalise(f, case.expected.get(f)):
                report.correct[f] += 1
            else:
                wrong[f] = {"expected": case.expected.get(f), "got": got[f]}
        report.category_confusion[(case.expected["category"], got["category"])] += 1
        if wrong:
            report.mistakes.append({"id": case.id, "wrong": wrong})
        else:
            report.correct["all"] += 1
    return report
