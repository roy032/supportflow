import pytest
from pydantic import ValidationError

from app.config import Settings


def make(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_placeholder_secret_counts_as_unset():
    assert make(tally_signing_secret="change-me").tally_signing_secret == ""


def test_secret_and_slack_url_are_stripped():
    s = make(tally_signing_secret="  abc \n", slack_webhook_url=" https://hooks.slack.test/x ")
    assert s.tally_signing_secret == "abc"
    assert s.slack_webhook_url == "https://hooks.slack.test/x"


def test_log_level_is_normalised_and_validated():
    assert make(log_level="debug").log_level == "DEBUG"
    with pytest.raises(ValidationError):
        make(log_level="verbose")


def test_timeout_must_be_positive():
    with pytest.raises(ValidationError):
        make(llm_timeout_seconds=0)
