"""OCR layer for bank statement PDFs.

Several Indian banks (HDFC included) ship statement PDFs with a broken
CID/embedded font map: the glyphs render correctly on screen but the
underlying text layer extracts as garbage or empty (`pdftotext` on an HDFC
statement returns nothing but the digital-signature footer). The reliable
path is to rasterize every page and OCR it.
"""
import pytesseract
from pdf2image import convert_from_path


def pdf_to_page_texts(pdf_path, dpi=300, psm=6):
    """Rasterize each page of pdf_path and OCR it.

    psm 6 ("uniform block of text") is required — the default PSM silently
    drops the amount columns on transaction rows in these statements.

    Returns a list[str], one entry per page, in page order.
    """
    images = convert_from_path(pdf_path, dpi=dpi)
    config = f"--psm {psm}"
    return [pytesseract.image_to_string(img, config=config) for img in images]
