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
                    file_hash=None, source="upload", email_date=None):
    """Parse a bank statement PDF and stage it for review.

    Returns the pending StatementImport. Raises ValueError if the PDF can't
    be read or no parser recognizes it.
    """
    bank, parsed = parse_pdf(stored_path, dpi=ocr_dpi)

    stmt_import = StatementImport(
        family_id=family_id, uploaded_by_id=user_id,
        bank=bank, original_filename=original_filename, stored_path=stored_path,
        file_hash=file_hash or file_sha256(stored_path),
        source=source, email_date=email_date,
        status=IMPORT_STATUS_PENDING, warnings="\n".join(parsed.warnings),
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
