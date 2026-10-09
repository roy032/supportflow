"""Webhook signature verification.

Tally signs every webhook with HMAC-SHA256 over the request body, using the form's
signing secret, and sends the base64-encoded digest in the `Tally-Signature` header.
We recompute the digest over the *raw* bytes we received and compare in constant time.
"""

import base64
import hashlib
import hmac


def compute_tally_signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_tally_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """Return True only if `signature` is a valid Tally signature for `body`.

    Rejects everything when the secret is missing, so a misconfigured server
    fails closed instead of accepting unsigned requests.
    """
    if not secret or not signature:
        return False
    expected = compute_tally_signature(body, secret).encode("ascii")
    # Compare bytes, not str: compare_digest raises TypeError on non-ASCII str input,
    # which would turn a forged header into a 500 instead of a 401.
    received = signature.strip().encode("utf-8", errors="replace")
    return hmac.compare_digest(expected, received)
