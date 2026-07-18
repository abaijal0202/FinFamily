"""Bank statement import: OCR + parse + review-before-confirm.

Usage:
    from statement_import import parse_pdf
    bank, parsed = parse_pdf("/path/to/statement.pdf")
"""
from .ocr import pdf_to_page_texts
from .registry import detect_bank, get_parser, supported_banks


def parse_pdf(pdf_path, dpi=300):
    """OCR a bank statement PDF and parse it with the matching bank plugin.

    Returns (bank_name, ParsedStatement).
    Raises ValueError if no registered parser recognizes the document.
    """
    page_texts = pdf_to_page_texts(pdf_path, dpi=dpi)
    bank = detect_bank(page_texts)
    if not bank:
        raise ValueError(
            "Could not identify the issuing bank from this statement. "
            f"Supported banks: {', '.join(supported_banks())}."
        )
    parser = get_parser(bank)
    return bank, parser.parse(page_texts)
