"""Gmail statement ingestion via IMAP (read-only).

Connects with a Google App Password (Settings > Security > 2-Step
Verification > App passwords), searches recent mail for statement emails,
downloads PDF attachments, and routes each one:

  - CAS PDF (CAMS/KFintech, opens with CAS_PASSWORD)  -> applied to MF assets
  - anything else                                     -> bank import pipeline,
                                                          staged for review

Every examined message is remembered in ProcessedEmail so re-checks are
cheap and nothing is imported twice. Nothing is ever modified or deleted
in the mailbox (IMAP is opened read-only).

.env keys:
  GMAIL_ADDRESS=you@gmail.com
  GMAIL_APP_PASSWORD=16-char app password
  CAS_PASSWORD=your CAS PDF password (usually PAN)
  GMAIL_SEARCH_DAYS=90                     (optional)
  GMAIL_SENDERS=comma,separated,extras     (optional, adds to defaults)
"""
import email
import email.utils
import imaplib
import os
import re
from datetime import datetime, timedelta

from models import db, ProcessedEmail

# Senders that commonly deliver Indian bank e-statements and MF CAS mails.
DEFAULT_SENDERS = [
    "camsonline.com",        # CAMS CAS
    "kfintech.com",          # KFintech CAS
    "hdfcbank.net",          # HDFC e-statements
    "hdfcbank.com",
    "icicibank.com",
    "axisbank.com",
    "sbi.co.in",
    "kotak.com",
]
SUBJECT_KEYWORDS = ["statement", "consolidated account", "cas "]


def _config(app):
    return {
        "address": os.environ.get("GMAIL_ADDRESS", ""),
        "app_password": os.environ.get("GMAIL_APP_PASSWORD", ""),
        "cas_password": os.environ.get("CAS_PASSWORD", ""),
        "days": int(os.environ.get("GMAIL_SEARCH_DAYS", "90")),
        "senders": DEFAULT_SENDERS + [
            s.strip().lower() for s in os.environ.get("GMAIL_SENDERS", "").split(",") if s.strip()
        ],
    }


def gmail_configured():
    return bool(os.environ.get("GMAIL_ADDRESS") and os.environ.get("GMAIL_APP_PASSWORD"))


def _matches(sender, subject, cfg):
    sender_l = (sender or "").lower()
    subject_l = (subject or "").lower()
    if any(s in sender_l for s in cfg["senders"]):
        return True
    return any(k in subject_l for k in SUBJECT_KEYWORDS)


def _decode(value):
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = ""
    for text, enc in parts:
        out += text.decode(enc or "utf-8", errors="replace") if isinstance(text, bytes) else text
    return out


def _pdf_attachments(msg):
    for part in msg.walk():
        fname = part.get_filename()
        if not fname:
            continue
        fname = _decode(fname)
        if fname.lower().endswith(".pdf"):
            payload = part.get_payload(decode=True)
            if payload:
                yield fname, payload


def check_gmail(app, family_id, user_id):
    """Search Gmail and ingest new statement PDFs.

    Returns a summary dict: {"checked": n, "bank_imports": [...], "cas": [...],
    "errors": [...]}. Raises RuntimeError if Gmail is not configured.
    """
    cfg = _config(app)
    if not cfg["address"] or not cfg["app_password"]:
        raise RuntimeError("Gmail is not configured — set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env")

    summary = {"checked": 0, "bank_imports": [], "cas": [], "errors": []}

    inbox_dir = os.path.join(app.config["UPLOAD_FOLDER"], str(family_id), "gmail")
    os.makedirs(inbox_dir, exist_ok=True)

    since = (datetime.now() - timedelta(days=cfg["days"])).strftime("%d-%b-%Y")

    conn = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        conn.login(cfg["address"], cfg["app_password"])
        conn.select("INBOX", readonly=True)
        _typ, data = conn.search(None, f'(SINCE "{since}")')
        msg_nums = data[0].split() if data and data[0] else []

        known = {p.message_id for p in ProcessedEmail.query.filter_by(family_id=family_id).all()}

        for num in msg_nums:
            _typ, header_data = conn.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT)])")
            if not header_data or not header_data[0]:
                continue
            headers = email.message_from_bytes(header_data[0][1])
            message_id = (headers.get("Message-ID") or "").strip()
            sender = _decode(headers.get("From"))
            subject = _decode(headers.get("Subject"))

            if not message_id or message_id in known:
                continue
            if not _matches(sender, subject, cfg):
                continue

            _typ, full_data = conn.fetch(num, "(BODY.PEEK[])")
            if not full_data or not full_data[0]:
                continue
            msg = email.message_from_bytes(full_data[0][1])

            summary["checked"] += 1
            results = []
            for fname, payload in _pdf_attachments(msg):
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", fname)
                stored = os.path.join(inbox_dir, f"{datetime.utcnow():%Y%m%d%H%M%S}_{safe}")
                with open(stored, "wb") as f:
                    f.write(payload)
                try:
                    results.append(_route_pdf(app, family_id, user_id, stored, fname, cfg))
                except Exception as exc:
                    msg_txt = f"{fname}: {exc}"
                    summary["errors"].append(msg_txt)
                    results.append(f"error: {msg_txt}")

            db.session.add(ProcessedEmail(
                family_id=family_id, message_id=message_id, sender=sender[:255],
                subject=subject[:500], result="; ".join(results)[:500] or "no pdf attachment",
            ))
            db.session.commit()
            known.add(message_id)

            # collect outcomes for the flash message
            for r in results:
                if r.startswith("cas:"):
                    summary["cas"].append(r)
                elif r.startswith("bank:"):
                    summary["bank_imports"].append(r)
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    return summary


def _route_pdf(app, family_id, user_id, stored_path, original_filename, cfg):
    """Duplicate check first, then CAS (cheap text-layer probe), else the
    bank OCR pipeline."""
    from statement_import.cas_import import apply_cas, is_cas_pdf
    from import_service import ingest_bank_pdf, file_sha256, find_existing_import

    file_hash = file_sha256(stored_path)
    existing = find_existing_import(family_id, file_hash)
    if existing:
        return (f"duplicate: identical PDF already imported on "
                f"{existing.uploaded_at:%d %b %Y} ({original_filename}) — skipped")

    if cfg["cas_password"] and is_cas_pdf(stored_path, cfg["cas_password"]):
        created, updated, skipped = apply_cas(
            family_id, user_id, stored_path, cfg["cas_password"], original_filename,
            file_hash=file_hash,
        )
        return f"cas: {created} new + {updated} updated folios ({original_filename})"

    stmt = ingest_bank_pdf(family_id, user_id, stored_path, original_filename,
                           ocr_dpi=app.config["OCR_DPI"], file_hash=file_hash)
    return f"bank: {stmt.bank} import #{stmt.id} pending review ({original_filename})"
