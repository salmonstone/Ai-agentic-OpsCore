"""Tests for _classify_secret_value — drives how severe a hardcoded env var is reported.

A misclassification here changes a finding's severity (critical vs. low), so
it directly decides whether a real leaked credential gets flagged loudly.
"""
import pytest

from agent.integrations.kubectl import _classify_secret_value


@pytest.mark.parametrize("value, expected", [
    ("AKIAIOSFODNN7EXAMPLE",                                      "aws_access_key"),
    ("https://hooks.slack.com/services/T000/B000/XXXX",           "slack_webhook"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIE...",                  "private_key"),
    ("-----BEGIN PRIVATE KEY-----",                               "private_key"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sig_part-123",      "jwt"),
])
def test_known_credential_formats_are_high_confidence(value, expected):
    assert _classify_secret_value(value) == (expected, "high")


@pytest.mark.parametrize("value", [
    "", "changeme", "CHANGEME", "  test  ", "password", "your-key-here", "<value>", "xxx",
])
def test_placeholders_are_low_confidence(value):
    assert _classify_secret_value(value) == ("placeholder", "low")


def test_long_mixed_string_is_medium_confidence():
    assert _classify_secret_value("aB3dE5fG7hJ9kL1mN3pQ5") == ("generic_high_entropy", "medium")


@pytest.mark.parametrize("value", [
    "aB3dE5",                        # mixed but too short
    "alllowercaseandlongenough123",  # no uppercase
    "ALLUPPERCASEANDLONGENOUGH123",  # no lowercase
    "NoDigitsHereButLongEnoughXyz",  # no digit
])
def test_ambiguous_values_are_low_confidence(value):
    assert _classify_secret_value(value) == ("unclassified", "low")


def test_aws_key_pattern_is_anchored():
    # A key embedded in other text, or with a wrong prefix, must not match
    # the high-confidence AWS pattern.
    assert _classify_secret_value("xAKIAIOSFODNN7EXAMPLE")[0] != "aws_access_key"
    assert _classify_secret_value("ASIAIOSFODNN7EXAMPLE")[0] != "aws_access_key"


def test_result_never_contains_the_value():
    secret = "AKIAIOSFODNN7EXAMPLE"
    assert secret not in "".join(_classify_secret_value(secret))
