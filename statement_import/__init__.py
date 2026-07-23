"""Bank statement import: OCR + parse + review-before-confirm.

Usage:
    from statement_import import parse_pdf
    bank, parsed = parse_pdf("/path/to/statement.pdf")
"""
from .ocr import pdf_to_page_texts
from .registry import detect_bank, get_parser, supported_banks


def parse_pdf(pdf_path, dpi=300, password=None):
    """OCR a bank statement PDF and parse it with the matching bank plugin.

    Some banks' statements OCR more accurately at different dpi/psm settings
    than the shared default (e.g. SBI's masked account number and ledger
    rows need `--psm 4` at 400dpi, not the default `--psm 6` at 300dpi) --
    a parser module can declare this via a `PREFERRED_OCR = {"dpi":.., "psm":..}`
    attribute, and once that bank is identified on the initial pass, the
    document is re-OCR'd once at those settings before parsing.

    Returns (bank_name, ParsedStatement).
    Raises ValueError if no registered parser recognizes the document.
    """
    page_texts = pdf_to_page_texts(pdf_path, dpi=dpi, password=password)
    bank = detect_bank(page_texts)
    if not bank:
        raise ValueError(
            "Could not identify the issuing bank from this statement. "
            f"Supported banks: {', '.join(supported_banks())}."
        )
    parser = get_parser(bank)
    preferred = getattr(parser, "PREFERRED_OCR", None)
    if preferred:
        page_texts = pdf_to_page_texts(
            pdf_path, dpi=preferred.get("dpi", dpi), psm=preferred.get("psm", 6),
            password=password,
        )
    return bank, parser.parse(page_texts)
