from app.security import compute_tally_signature, verify_tally_signature

BODY = b'{"eventId":"abc","eventType":"FORM_RESPONSE"}'


def test_valid_signature_is_accepted():
    sig = compute_tally_signature(BODY, "secret")
    assert verify_tally_signature(BODY, sig, "secret")


def test_wrong_secret_is_rejected():
    sig = compute_tally_signature(BODY, "other-secret")
    assert not verify_tally_signature(BODY, sig, "secret")


def test_tampered_body_is_rejected():
    sig = compute_tally_signature(BODY, "secret")
    assert not verify_tally_signature(BODY + b" ", sig, "secret")


def test_missing_signature_or_secret_fails_closed():
    sig = compute_tally_signature(BODY, "secret")
    assert not verify_tally_signature(BODY, None, "secret")
    assert not verify_tally_signature(BODY, sig, "")


def test_non_ascii_signature_is_rejected_not_raised():
    # Header values can carry non-ASCII bytes; this must be a clean rejection.
    assert not verify_tally_signature(BODY, "sigéÿ", "secret")


def test_surrounding_whitespace_in_header_is_ignored():
    sig = compute_tally_signature(BODY, "secret")
    assert verify_tally_signature(BODY, f"  {sig} ", "secret")
