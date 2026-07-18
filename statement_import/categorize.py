"""Rule-based transaction auto-categorization (BRD FR-BANK-04).

Deliberately simple keyword matching for the MVP. Users can override the
category per-transaction on the import review screen; a future phase can
retrain/reorder these rules from those corrections.
"""

# Ordered: first matching rule wins, so more specific keywords go first.
_CATEGORY_RULES = [
    (("SALARY",), "Salary"),
    (("INTEREST PAID", "INTEREST CREDIT", "INT.PD", "INT PD"), "Interest Income"),
    (("DIVIDEND",), "Dividend / Auto-Credit"),
    (("BLINKIT", "SWIGGY", "ZOMATO", "ZEPTO", "BIGBASKET", "GROCER"), "Food & Groceries"),
    (("ATM", "CASH WDL", "CASH WITHDRAWAL"), "Cash Withdrawal"),
    (("BILLPAY", "BILL PAY", "ELECTRICITY", "RECHARGE"), "Bills & Utilities"),
    (("IB FUNDS TRANSFER", "FUNDS TRANSFER", "OWN ACCOUNT"), "Internal Transfer"),
    (("UPI-",), "UPI Transfer"),
    (("POS ",), "Shopping / POS"),
    (("ACH C-", "ACH CR"), "Dividend / Auto-Credit"),
    (("NEFT CR", "RTGS CR", "IMPS"), "Transfer In"),
    (("NEFT DR", "RTGS DR"), "Transfer Out"),
]


def categorize(narration):
    """Best-effort category for a transaction narration string."""
    n = (narration or "").upper()
    for keywords, category in _CATEGORY_RULES:
        if any(k in n for k in keywords):
            return category
    return "Uncategorized"
