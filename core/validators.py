"""Shared validators for Indian mobile numbers and PIN codes.

Checkout, saved addresses, the dashboard address editor and the India Post booking
all call these, so a value accepted in one place is never rejected in another.
"""

from __future__ import annotations

import re

from django.core.exceptions import ValidationError

MOBILE_ERROR = "Enter a valid 10-digit Indian mobile number starting with 6-9."
PINCODE_ERROR = "Enter a valid 6-digit PIN code."

_MOBILE_RE = re.compile(r"[6-9]\d{9}")
_PINCODE_RE = re.compile(r"[1-9]\d{5}")


def normalize_indian_mobile(raw: object) -> str:
    """
    Return the 10-digit national mobile number, or "" when ``raw`` is not a valid one.

    Accepts the common ways customers type a number: ``9876543210``, ``+91 98765 43210``,
    ``919876543210`` and ``09876543210``. The country code is only stripped when it is
    unambiguous (``+91`` prefix, or ``91`` on a 12-digit number) — a plain 10-digit number
    that merely *starts* with 91 (e.g. ``9123456789``) is a valid mobile and is kept whole.
    """
    value = re.sub(r"[\s\-()]", "", str(raw or ""))
    if value.startswith("+"):
        if not value.startswith("+91"):
            return ""
        value = value[3:]
    elif len(value) == 12 and value.startswith("91"):
        value = value[2:]
    elif len(value) == 11 and value.startswith("0"):
        value = value[1:]
    return value if _MOBILE_RE.fullmatch(value) else ""


def normalize_pincode(raw: object) -> str:
    """Return the 6-digit PIN code, or "" when ``raw`` is not a valid Indian PIN."""
    value = str(raw or "").strip()
    return value if _PINCODE_RE.fullmatch(value) else ""


POST_OFFICE_MIN_LENGTH = 3
POST_OFFICE_MAX_LENGTH = 80


def post_office_error(raw: object) -> str:

    value = str(raw or "").strip()
    if not value:
        return "Post Office is required."
    if not re.search(r"[a-zA-Z]", value):
        return "Enter a valid post office name."
    if not POST_OFFICE_MIN_LENGTH <= len(value) <= POST_OFFICE_MAX_LENGTH:
        return f"Post Office must be between {POST_OFFICE_MIN_LENGTH} and {POST_OFFICE_MAX_LENGTH} characters."
    return ""


def validate_indian_mobile(value: object) -> None:
    if not normalize_indian_mobile(value):
        raise ValidationError(MOBILE_ERROR, code="invalid_mobile")


def validate_indian_pincode(value: object) -> None:
    if not normalize_pincode(value):
        raise ValidationError(PINCODE_ERROR, code="invalid_pincode")
