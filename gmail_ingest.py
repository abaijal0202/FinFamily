"""Gmail statement ingestion via IMAP (read-only).

Connects with a Google App Password (Settings > Security > 2-Step
Verification > App passwords), searches recent mail for statement emails,
downloads PDF attachments, and routes each one:

  - CAS PDF (CAMS/KFintech or NSDL, opens with        -> applied to MF/equity
    CAS_PASSWORD)                                         assets
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
import threading
from datetime import datetime, timedelta

from models import db, ProcessedEmail, ScanSender

# Only one Gmail check may run at a time (prevents duplicate ProcessedEmail
# inserts when a second check starts while the first is still working).
_check_lock = threading.Lock()

# Senders that commonly deliver Indian bank e-statements and MF CAS mails.
# Deliberately narrow: nsdl.com is NOT here — evoting@nsdl.com sends ballot
# notices with PDF attachments that are not statements.
DEFAULT_SENDERS = [
    "camsonline.com",        # CAMS CAS
    "kfintech.com",          # KFintech CAS
    "nsdl.co.in",            # NSDL e-CAS (depository CAS)
    "hdfcbank.net",          # HDFC e-statements
    "hdfcbank.com",
    "icicibank.com",
    "axisbank.com",
    "sbi.co.in",
    "kotak.com",
]
SUBJECT_KEYWORDS = ["statement", "consolidated account", "cas "]


def _config(app, family_id=None):
    """Env config merged with the family's Import Settings (ScanSender rows).

    sender_passwords: list of (sender_fragment_lower, password) — tried for
    password-protected attachments from that sender, before CAS_PASSWORD.
    """
    db_senders, sender_passwords = [], []
    if family_id is not None:
        for s in ScanSender.query.filter_by(family_id=family_id, active=True).all():
            frag = (s.email or "").strip().lower()
            if frag:
                db_senders.append(frag)
                if s.attachment_password:
                    sender_passwords.append((frag, s.attachment_password))
    return {
        "address": os.environ.get("GMAIL_ADDRESS", ""),
        "app_password": os.environ.get("GMAIL_APP_PASSWORD", ""),
        "cas_password": os.environ.get("CAS_PASSWORD", ""),
        "days": int(os.environ.get("GMAIL_SEARCH_DAYS", "90")),
        "senders": DEFAULT_SENDERS + db_senders + [
            s.strip().lower() for s in os.environ.get("GMAIL_SENDERS", "").split(",") if s.strip()
        ],
        "sender_passwords": sender_passwords,
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
    cfg = _config(app, family_id=family_id)
    if not cfg["address"] or not cfg["app_password"]:
        raise RuntimeError("Gmail is not configured — set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env")

    if not _check_lock.acquire(blocking=False):
        raise RuntimeError("A Gmail check is already running — wait for it to finish, then refresh the page.")
    try:
        return _check_gmail_locked(app, family_id, user_id, cfg)
    finally:
        _check_lock.release()


def _check_gmail_locked(app, family_id, user_id, cfg):
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
            _typ, header_data = conn.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])")
            if not header_data or not header_data[0]:
                continue
            headers = email.message_from_bytes(header_data[0][1])
            message_id = (headers.get("Message-ID") or "").strip()
            sender = _decode(headers.get("From"))
            subject = _decode(headers.get("Subject"))
            email_date = None
            try:
                dt = email.utils.parsedate_to_datetime(headers.get("Date"))
                if dt is not None:
                    email_date = dt.replace(tzinfo=None) if dt.tzinfo is None \
                        else dt.astimezone(tz=None).replace(tzinfo=None)
            except (TypeError, ValueError):
                pass

            if not message_id or message_id in known:
                continue
            if not _matches(sender, subject, cfg):
                continue

            # Sender-specific attachment password from Import Settings, if any
            sender_l = sender.lower()
            sender_pw = next((pw for frag, pw in cfg["sender_passwords"] if frag in sender_l), None)

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
                    results.append(_route_pdf(app, family_id, user_id, stored, fname, cfg,
                                              email_date=email_date, sender_pw=sender_pw))
                except Exception as exc:
                    msg_txt = f"{fname}: {exc}"
                    summary["errors"].append(msg_txt)
                    results.append(f"error: {msg_txt}")

            try:
                db.session.add(ProcessedEmail(
                    family_id=family_id, message_id=message_id, sender=sender[:255],
                    subject=subject[:500], result="; ".join(results)[:500] or "no pdf attachment",
                ))
                db.session.commit()
            except Exception:
                # Unique-constraint race (e.g. a concurrent/previous check already
                # recorded this message). The work above is idempotent thanks to
                # the PDF duplicate guard, so just move on.
                db.session.rollback()
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


def _route_pdf(app, family_id, user_id, stored_path, original_filename, cfg,
               email_date=None, sender_pw=None):
    """Delegate to the shared router. Candidate passwords for opening a
    protected attachment: the sender-specific one (Import Settings) first,
    then the global CAS_PASSWORD from .env."""
    from import_service import route_pdf

    result = route_pdf(
        app, family_id, user_id, stored_path, original_filename,
        source="gmail", email_date=email_date,
        passwords=[sender_pw, cfg["cas_password"]],
    )
    kind = result["kind"]
    if kind == "duplicate":
        return f"duplicate: already imported ({original_filename}) — skipped"
    if kind == "cas":
        created, updated, _ = result["counts"]
        return f"cas: {created} new + {updated} updated folios ({original_filename})"
    if kind == "skipped":
        return f"skipped: no account/transaction data found in {original_filename}"
    return f"bank: {result['stmt'].bank} import #{result['stmt'].id} pending review ({original_filename})"
