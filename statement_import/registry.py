"""Plugin registry for bank statement parsers.

To add a bank with a dedicated, tuned parser: write a `parsers/<bank>.py`
module exposing `detect(page_texts) -> bool` and
`parse(page_texts) -> ParsedStatement` (see `parsers/hdfc.py` for a
validated example), then register it in `_BANK_PARSERS` below.

Statements from banks without a dedicated parser aren't rejected -- they
fall through to `parsers/generic.py`, a best-effort bank-agnostic reader.
Accuracy there is lower (flagged via warnings on the parsed statement),
but nothing is blocked on a specific bank being "supported": every field
can be corrected on the review screen regardless of which parser ran.
"""
from .parsers import hdfc, generic

_BANK_PARSERS = [
    ("HDFC", hdfc),
]
_FALLBACK_NAME = "Generic"
_FALLBACK_MODULE = generic


def detect_bank(page_texts):
    for name, module in _BANK_PARSERS:
        if module.detect(page_texts):
            return name
    return _FALLBACK_NAME


def get_parser(bank_name):
    for name, module in _BANK_PARSERS:
        if name == bank_name:
            return module
    if bank_name == _FALLBACK_NAME:
        return _FALLBACK_MODULE
    return None


def supported_banks():
    """Banks with a dedicated, validated parser (others still work via the
    generic fallback, just with lower confidence)."""
    return [name for name, _ in _BANK_PARSERS]
