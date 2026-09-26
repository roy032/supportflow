"""Classify one message with your local Ollama model and print the result.

Useful to check Ollama is running and to see how the model handles a message
before wiring up the whole pipeline.

Usage:
    python scripts/try_classifier.py "Where is my order ORD-12?"
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.classifier import ClassificationError, classify  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.llm import get_provider  # noqa: E402
from app.tally import SupportMessage  # noqa: E402


def main() -> int:
    text = " ".join(sys.argv[1:]) or "আমার অর্ডার ORD-77 এখনো আসেনি, কবে পাবো?"
    provider = get_provider(get_settings())
    print(f"Model: {provider.name}\nMessage: {text}\n")
    try:
        outcome = classify(provider, SupportMessage(message=text))
    except ClassificationError as exc:
        print(f"Failed: {exc}")
        return 1
    print(json.dumps(outcome.result.model_dump(), indent=2, ensure_ascii=False))
    print(f"\n{outcome.latency_ms} ms, {outcome.attempts} attempt(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
