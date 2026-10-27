"""Eval harness scoring, with a fake model."""

import json

from app.evaluation import Case, load_cases, price_for, run_eval
from app.llm.base import LLMResult
from tests.test_classifier import FakeProvider

GOOD = {"category": "refund", "urgency": "high", "sentiment": "negative", "language": "en",
        "order_id": "ORD-1", "summary": "Wants a refund."}


def case(cid, **expected):
    return Case(id=cid, message="m", expected={**GOOD, **expected})


def test_bundled_cases_are_well_formed():
    from pathlib import Path

    from app.classifier import ClassificationResult

    cases = load_cases(Path(__file__).resolve().parents[1] / "evals" / "classification.jsonl")
    assert len(cases) >= 30
    assert len({c.id for c in cases}) == len(cases)
    languages = {c.expected["language"] for c in cases}
    assert languages == {"en", "bn", "mixed"}
    for c in cases:
        ClassificationResult.model_validate({**c.expected, "summary": "x"})


def test_scores_fields_confusion_tokens_and_cost():
    answers = [
        LLMResult(json.dumps(GOOD), input_tokens=1000, output_tokens=100),
        LLMResult(json.dumps({**GOOD, "category": "complaint", "order_id": "ord-1"}),
                  input_tokens=1000, output_tokens=100),
    ]
    provider = FakeProvider(*answers)
    provider.name = "anthropic:claude-opus-5-5"
    report = run_eval(provider, [case("a"), case("b")]).as_dict()

    assert report["accuracy"]["category"] == 0.5
    assert report["accuracy"]["order_id"] == 1.0  # compared case-insensitively
    assert report["exact_match"] == 0.5
    assert {"expected": "refund", "got": "complaint", "count": 1} in report["category_confusion"]
    assert report["mistakes"] == [
        {"id": "b", "wrong": {"category": {"expected": "refund", "got": "complaint"}}}
    ]
    assert report["tokens"] == {"input": 2000, "output": 200}
    # 2000 * $4/M + 200 * $20/M
    assert report["cost_usd"]["total"] == 0.012


def test_failed_cases_count_as_wrong():
    report = run_eval(FakeProvider("bad", "bad"), [case("x")]).as_dict()
    assert report["failed"] == 1
    assert report["accuracy"]["category"] == 0.0
    assert report["errors"][0]["id"] == "x"


def test_local_models_are_free():
    assert price_for("ollama:qwen2.5:3b") == (0.0, 0.0)
    assert price_for("anthropic:claude-haiku-5-5") == (0.10, 0.50)
