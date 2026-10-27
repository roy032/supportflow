"""Score a classifier model against the labelled messages in evals/classification.jsonl.

Usage:
    python scripts/run_eval.py                                   # LLM_PROVIDER from .env
    python scripts/run_eval.py --provider ollama --model qwen2.5:7b
    python scripts/run_eval.py --provider anthropic --model claude-opus-5-5
    python scripts/run_eval.py --json-out report.json --min-category-accuracy 0.9

Reports accuracy for each field, exact-match rate, p50/p95 latency, tokens, cost, the
category confusion pairs and every mistake. Exits with status 1 when category accuracy is
below --min-category-accuracy, so it can gate a model or prompt change.

Running against a cloud provider sends every message to it and costs money (a few cents
for the bundled 30 messages).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.evaluation import FIELDS, load_cases, run_eval  # noqa: E402
from app.llm import get_provider  # noqa: E402

DEFAULT_CASES = Path(__file__).resolve().parents[1] / "evals" / "classification.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--provider", choices=["ollama", "anthropic"])
    parser.add_argument("--model", help="model for the chosen provider")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--json-out", help="also write the full report as JSON here")
    parser.add_argument("--min-category-accuracy", type=float, default=0.0)
    args = parser.parse_args()

    overrides: dict[str, str] = {"llm_fallback_provider": ""}  # measure one model only
    if args.provider:
        overrides["llm_provider"] = args.provider
    provider_name = args.provider or get_settings().llm_provider
    if args.model:
        overrides[f"{provider_name}_model"] = args.model
    settings = get_settings().model_copy(update=overrides)

    cases = load_cases(args.cases)
    provider = get_provider(settings)
    print(f"Evaluating {provider.name} on {len(cases)} messages...")
    report = run_eval(provider, cases).as_dict()

    print(f"\n{'field':<10} accuracy")
    for f in FIELDS:
        print(f"{f:<10} {report['accuracy'][f]:.1%}")
    print(f"{'all':<10} {report['exact_match']:.1%}  (every field right)")
    lat = report["latency_ms"]
    print(f"\nlatency    p50 {lat['p50']} ms, p95 {lat['p95']} ms")
    print(f"tokens     {report['tokens']['input']} in, {report['tokens']['output']} out")
    cost = report["cost_usd"]
    print(f"cost       ${cost['total']:.4f} total, ${cost['per_1000_messages'] or 0:.2f} per 1,000 messages")
    confused = [c for c in report["category_confusion"] if c["expected"] != c["got"]]
    if confused:
        print("\ncategory mix-ups:")
        for c in confused:
            print(f"  {c['expected']} -> {c['got']}: {c['count']}")
    if report["errors"]:
        print(f"\n{len(report['errors'])} message(s) failed to classify:")
        for e in report["errors"]:
            print(f"  {e['id']}: {e['error']}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"\nFull report: {args.json_out}")

    if report["accuracy"]["category"] < args.min_category_accuracy:
        print(f"\nFAIL: category accuracy below {args.min_category_accuracy:.0%}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
