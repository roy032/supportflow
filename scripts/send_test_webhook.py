"""Send signed sample webhooks to a running SupportFlow server, just like Tally would.

Usage (with the server running on port 8000):
    python scripts/send_test_webhook.py                # all three samples
    python scripts/send_test_webhook.py --sample bangla
    python scripts/send_test_webhook.py --url https://<your-ngrok-url>/webhooks/tally

Uses TALLY_SIGNING_SECRET from your .env, so the server accepts the requests.
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.security import compute_tally_signature  # noqa: E402

SAMPLES = {
    "english": {
        "name": "Sarah Ahmed",
        "email": "sarah@example.com",
        "order": "ORD-2041",
        "message": "I ordered a pair of headphones 10 days ago and it still hasn't arrived. "
        "The tracking page hasn't updated in a week. Can you tell me where it is?",
    },
    "bangla": {
        "name": "রহিম উদ্দিন",
        "email": "rahim@example.com",
        "order": "ORD-3310",
        "message": "আমি যে শার্টটা অর্ডার করেছিলাম সেটা ভুল সাইজ এসেছে। আমি টাকা ফেরত চাই, খুব দ্রুত।",
    },
    "spam": {
        "name": "Best Deals",
        "email": "promo@spam.example",
        "order": "",
        "message": "CONGRATULATIONS!!! You won a free iPhone. Click here to claim your prize now!!!",
    },
}


def build_payload(sample: dict[str, str]) -> dict:
    return {
        "eventId": str(uuid.uuid4()),
        "eventType": "FORM_RESPONSE",
        "createdAt": "2026-09-26T12:00:00.000Z",
        "data": {
            "responseId": uuid.uuid4().hex[:8],
            "formId": "test-form",
            "formName": "Contact support",
            "fields": [
                {"key": "q_name", "label": "Full name", "type": "INPUT_TEXT", "value": sample["name"]},
                {"key": "q_email", "label": "Email", "type": "INPUT_EMAIL", "value": sample["email"]},
                {"key": "q_order", "label": "Order ID", "type": "INPUT_TEXT", "value": sample["order"] or None},
                {"key": "q_msg", "label": "Message", "type": "TEXTAREA", "value": sample["message"]},
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8000/webhooks/tally")
    parser.add_argument("--sample", choices=[*SAMPLES, "all"], default="all")
    args = parser.parse_args()

    secret = get_settings().tally_signing_secret
    if not secret or secret == "change-me":
        print("Set TALLY_SIGNING_SECRET in your .env first.")
        return 1

    names = list(SAMPLES) if args.sample == "all" else [args.sample]
    for name in names:
        body = json.dumps(build_payload(SAMPLES[name]), ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Tally-Signature": compute_tally_signature(body, secret),
        }
        try:
            response = httpx.post(args.url, content=body, headers=headers, timeout=30)
        except httpx.HTTPError as exc:
            print(f"{name:8} could not reach {args.url}: {exc}")
            return 1
        print(f"{name:8} -> HTTP {response.status_code} {response.text}")

    print("\nCheck progress at http://localhost:8000/events and /failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
