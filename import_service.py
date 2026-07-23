"""Shared bank-statement ingestion: one code path whether the PDF arrived
via the web upload form or the Gmail checker.

Parses the PDF (OCR + bank plugin), records a pending StatementImport with
its ImportedAccount/ImportedTransaction rows, and returns it for review.
"""
import hashlib
import os
from datetime import datetime

from models import (
    db, Asset, StatementImport, ImportedAccount, ImportedTransaction, Transaction,
    IMPORT_STATUS_PENDING, IMPORT_STATUS_DISCARDED,
)
from statement_import import parse_pdf
from statement_import.categorize import categorize


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_existing_import(family_id, file_hash):
    """A non-discarded import of the identical PDF for this family, if any."""
    if not file_hash:
        return None
    return StatementImport.query.filter(
        StatementImport.family_id == family_id,
        StatementImport.file_hash == file_hash,
        StatementImport.status != IMPORT_STATUS_DISCARDED,
    ).first()


def diff_bank_accounts(existing_import, parsed):
    """Compare a previously-ingested StatementImport's ImportedAccount rows
    against a freshly re-parsed ParsedStatement of the same (or a corrected)
    PDF. Used to show a user what re-uploading a duplicate file would
    change before they decide whether to override the duplicate guard --
    e.g. after a parser fix picks up a fixed deposit or transactions that
    were silently missed the first time.

    Matches accounts/FDs by (kind, last-4-of-account-number); anything
    without a readable account number is bucketed under last4 "?" and will
    show as changed/added/removed together rather than matched precisely.

    Returns a list of dicts sorted by (kind, last4):
      {"kind": "BANK"|"FD", "last4": str, "status": "added"|"removed"|"changed"|"unchanged",
       "old": {...} | None, "new": {...} | None}
    Each present dict has: account_number, opening_balance, closing_balance,
    interest_rate, maturity_date, transaction_count.
    """
    def _round(x):
        return round(x, 2) if isinstance(x, (int, float)) else x

    old_by_key = {}
    for acc in existing_import.accounts:
        key = (acc.account_kind or "BANK", acc.account_number_last4 or "?")
        old_by_key[key] = {
            "account_number": acc.account_number,
            "opening_balance": _round(acc.opening_balance),
            "closing_balance": _round(acc.closing_balance),
            "interest_rate": _round(acc.interest_rate),
            "maturity_date": acc.maturity_date,
            # FDs never have ImportedTransaction rows -- keep this None for
            # them too so it doesn't manufacture a "changed" diff against
            # the parser's own None for FDs (see new_by_key below).
            "transaction_count": len(acc.transactions) if acc.account_kind == "BANK" else None,
        }

    new_by_key = {}
    for acc in parsed.accounts:
        last4 = acc.account_number[-4:] if acc.account_number else "?"
        new_by_key[("BANK", last4)] = {
            "account_number": acc.account_number,
            "opening_balance": _round(acc.opening_balance),
            "closing_balance": _round(acc.closing_balance),
            "interest_rate": None,
            "maturity_date": None,
            "transaction_count": len(acc.transactions),
        }
    for fd in parsed.fixed_deposits:
        last4 = fd.fd_number[-4:] if fd.fd_number else "?"
        new_by_key[("FD", last4)] = {
            "account_number": fd.fd_number,
            "opening_balance": _round(fd.principal),
            "closing_balance": _round(fd.current_amount),
            "interest_rate": _round(fd.rate),
            "maturity_date": fd.maturity_date,
            "transaction_count": None,
        }

    rows = []
    for key in sorted(set(old_by_key) | set(new_by_key)):
        kind, last4 = key
        old = old_by_key.get(key)
        new = new_by_key.get(key)
        if old is None:
            status = "added"
        elif new is None:
            status = "removed"
        elif old == new:
            status = "unchanged"
        else:
            status = "changed"
        rows.append({"kind": kind, "last4": last4, "status": status, "old": old, "new": new})
    return rows


def backfill_hashes_and_discard_duplicates():
    """One-off maintenance, run at startup: hash any imports that predate the
    duplicate guard, then auto-discard pending re-imports of the same PDF
    (keeping the earliest per family+hash; confirmed imports are never touched).
    Returns (hashed, discarded)."""
    hashed = 0
    for imp in StatementImport.query.filter(StatementImport.file_hash.is_(None)).all():
        if imp.stored_path and os.path.exists(imp.stored_path):
            try:
                imp.file_hash = file_sha256(imp.stored_path)
                hashed += 1
            except OSError:
                continue

    discarded = 0
    seen = {}  # (family_id, hash) -> keeper import
    q = StatementImport.query.filter(
        StatementImport.file_hash.isnot(None),
        StatementImport.status != IMPORT_STATUS_DISCARDED,
    ).order_by(StatementImport.uploaded_at)
    for imp in q.all():
        key = (imp.family_id, imp.file_hash)
        if key not in seen:
            seen[key] = imp
            continue
        if imp.status == IMPORT_STATUS_PENDING:  # never auto-touch confirmed imports
            imp.status = IMPORT_STATUS_DISCARDED
            imp.warnings = ((imp.warnings + "\n") if imp.warnings else "") + \
                "Auto-discarded: duplicate of an earlier import of the same PDF."
            discarded += 1
    db.session.commit()
    return hashed, discarded


def ingest_bank_pdf(family_id, user_id, stored_path, original_filename, ocr_dpi=300,
                    file_hash=None, source="upload", email_date=None, password=None,
                    asset_owner_id=None, supersedes_import_id=None):
    """Parse a bank statement PDF and stage it for review.

    `asset_owner_id` (defaults to the uploader, `user_id`) records which
    family member the parsed accounts belong to — used later at confirm
    time so an Owner/Contributor can import a family member's statement
    without logging in as them.

    `supersedes_import_id`, when set, records that this import was created
    by explicitly overriding an earlier duplicate-file warning (see
    `diff_bank_accounts` and `route_pdf`'s `allow_duplicate` flag).

    Returns the pending StatementImport. Raises ValueError if the PDF can't
    be read or no parser recognizes it.
    """
    bank, parsed = parse_pdf(stored_path, dpi=ocr_dpi, password=password)

    stmt_import = StatementImport(
        family_id=family_id, uploaded_by_id=user_id,
        asset_owner_id=asset_owner_id or user_id,
        bank=bank, original_filename=original_filename, stored_path=stored_path,
        file_hash=file_hash or file_sha256(stored_path),
        source=source, email_date=email_date,
        status=IMPORT_STATUS_PENDING, warnings="\n".join(parsed.warnings),
        supersedes_import_id=supersedes_import_id,
    )
    db.session.add(stmt_import)
    db.session.flush()

    existing_assets = Asset.query.filter(
        Asset.family_id == family_id, Asset.category.in_(["BANK", "FD"])
    ).all()
    by_last4 = {a.account_number_last4: a for a in existing_assets if a.account_number_last4}

    total_txns = 0
    for acc in parsed.accounts:
        last4 = acc.account_number[-4:] if acc.account_number else None
        matched = by_last4.get(last4)
        imp_acc = ImportedAccount(
            statement_import_id=stmt_import.id, account_kind="BANK",
            account_number=acc.account_number, account_number_last4=last4,
            account_type=acc.account_type, opening_balance=acc.opening_balance,
            closing_balance=acc.closing_balance, debit_total=acc.debit_total,
            credit_total=acc.credit_total, matched_asset_id=matched.id if matched else None,
            suggested_name=f"{bank} Savings ••{last4}" + (f" ({acc.account_type})" if acc.account_type else ""),
        )
        db.session.add(imp_acc)
        db.session.flush()

        for t in acc.transactions:
            is_dup = False
            if matched:
                is_dup = db.session.query(Transaction.id).filter_by(
                    asset_id=matched.id, txn_date=t.txn_date,
                    withdrawal=t.withdrawal, deposit=t.deposit,
                ).first() is not None
            db.session.add(ImportedTransaction(
                imported_account_id=imp_acc.id, txn_date=t.txn_date,
                narration=(t.narration or "")[:500], withdrawal=t.withdrawal, deposit=t.deposit,
                balance_after=t.balance_after, category=categorize(t.narration),
                is_duplicate=is_dup, include=not is_dup,
            ))
            total_txns += 1

    for fd in parsed.fixed_deposits:
        last4 = fd.fd_number[-4:] if fd.fd_number else None
        matched = by_last4.get(last4)
        db.session.add(ImportedAccount(
            statement_import_id=stmt_import.id, account_kind="FD",
            account_number=fd.fd_number, account_number_last4=last4,
            opening_balance=fd.principal, closing_balance=fd.current_amount,
            interest_rate=fd.rate, maturity_date=fd.maturity_date,
            matched_asset_id=matched.id if matched else None,
            suggested_name=f"{bank} FD ••{last4}",
        ))

    stmt_import.accounts_found = len(parsed.accounts) + len(parsed.fixed_deposits)
    stmt_import.transactions_found = total_txns
    db.session.commit()
    return stmt_import


def route_pdf(app, family_id, user_id, stored_path, original_filename, *,
              source="upload", email_date=None, passwords=None, ocr_dpi=None,
              asset_owner_id=None, allow_duplicate=False, supersedes_import_id=None):
    """Shared PDF ingestion used by both the web upload and the Gmail scan.

    1. Reject if the identical PDF was already imported (duplicate guard) --
       unless `allow_duplicate` is set, which lets a user explicitly
       re-import a PDF they've already uploaded (e.g. after a parser fix,
       or to re-check a statement that looked incomplete the first time).
       Callers that want to show the user what changed before re-importing
       should call `diff_bank_accounts()` first.
    2. Try to open it as a CAMS/KFintech CAS, trying each candidate password
       (and no password) — if it opens, apply it to MF holdings.
    3. Otherwise treat it as a bank statement: OCR it (using the first
       supplied password to unlock the PDF, if any) and stage for review.

    `user_id` is who performed the upload (recorded as uploaded_by_id, for
    the audit trail). `asset_owner_id` is who the holdings belong to —
    defaults to `user_id` — so an Owner/Contributor can import a family
    member's statement (CAS, NPS, bank) without logging in as them.

    Returns a dict: {"kind": "duplicate"|"cas"|"nps"|"bank"|"skipped",
                     "message": str, "stmt": StatementImport|None,
                     "counts": (created, updated, skipped) for CAS/NPS}.
    """
    from statement_import.cas_import import apply_cas, is_cas_pdf
    from statement_import.nsdl_cas import apply_nsdl_cas, is_nsdl_cas_pdf
    from statement_import.nps_kfintech import apply_nps, is_nps_pdf
    from statement_import.epf_epfo import apply_epf, is_epf_pdf

    ocr_dpi = ocr_dpi if ocr_dpi is not None else app.config["OCR_DPI"]
    passwords = [p for p in (passwords or []) if p]
    owner_id = asset_owner_id or user_id

    file_hash = file_sha256(stored_path)
    existing = None if allow_duplicate else find_existing_import(family_id, file_hash)
    if existing:
        return {"kind": "duplicate", "stmt": None, "counts": None, "existing_import": existing,
                "message": (f"identical PDF already imported on "
                            f"{existing.uploaded_at:%d %b %Y} — skipped")}

    # CAS probe with each candidate password, then no password. Tries the
    # CAMS/KFintech text-layer path first (cheap — no OCR), then the
    # OCR-based NSDL e-CAS path.
    seen = set()
    for pw in [*passwords, ""]:
        if pw in seen:
            continue
        seen.add(pw)
        if is_cas_pdf(stored_path, pw):
            created, updated, skipped = apply_cas(
                family_id, owner_id, stored_path, pw, original_filename,
                file_hash=file_hash, source=source, email_date=email_date,
            )
            return {"kind": "cas", "stmt": None, "counts": (created, updated, skipped),
                    "message": f"CAS applied: {created} new + {updated} updated folio(s)"}
        if is_nsdl_cas_pdf(stored_path, pw):
            created, updated, skipped = apply_nsdl_cas(
                family_id, owner_id, stored_path, pw, original_filename,
                file_hash=file_hash, source=source, email_date=email_date,
                dpi=ocr_dpi,
            )
            return {"kind": "cas", "stmt": None, "counts": (created, updated, skipped),
                    "message": f"NSDL CAS applied: {created} new + {updated} updated holding(s)"}
        if is_nps_pdf(stored_path, pw):
            created, updated, skipped = apply_nps(
                family_id, owner_id, stored_path, pw, original_filename,
                file_hash=file_hash, source=source, email_date=email_date,
            )
            return {"kind": "nps", "stmt": None, "counts": (created, updated, skipped),
                    "message": f"NPS statement applied: {created} new + {updated} updated holding(s)"}
        if is_epf_pdf(stored_path, pw):
            created, updated, skipped = apply_epf(
                family_id, owner_id, stored_path, pw, original_filename,
                file_hash=file_hash, source=source, email_date=email_date,
            )
            return {"kind": "epf", "stmt": None, "counts": (created, updated, skipped),
                    "message": f"EPF passbook applied: {created} new + {updated} updated account(s)"}

    # Bank statement OCR (first supplied password unlocks the PDF if needed).
    stmt = ingest_bank_pdf(family_id, user_id, stored_path, original_filename,
                           ocr_dpi=ocr_dpi, file_hash=file_hash,
                           source=source, email_date=email_date,
                           password=passwords[0] if passwords else None,
                           asset_owner_id=owner_id, supersedes_import_id=supersedes_import_id)
    if not stmt.accounts_found and not stmt.transactions_found:
        db.session.delete(stmt)
        db.session.commit()
        return {"kind": "skipped", "stmt": None, "counts": None,
                "message": f"no account/transaction data found in {original_filename}"}
    return {"kind": "bank", "stmt": stmt, "counts": None,
            "message": f"{stmt.bank} statement parsed — {stmt.accounts_found} account(s)"}
