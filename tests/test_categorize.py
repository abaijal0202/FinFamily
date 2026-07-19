from statement_import.categorize import categorize


def test_common_narrations():
    assert categorize("SALARY JUL 2026 ACME CORP") == "Salary"
    assert categorize("UPI-SWIGGY-ORDER123") == "Food & Groceries"
    assert categorize("ATM WDL MUMBAI") == "Cash Withdrawal"
    assert categorize("INTEREST PAID TILL 30-JUN") == "Interest Income"
    assert categorize("NEFT CR SOMEONE") == "Transfer In"


def test_specific_rule_wins_over_generic_upi():
    # Swiggy arrives via UPI, but the food rule is ordered before "UPI-"
    assert categorize("UPI-ZOMATO-XYZ") == "Food & Groceries"


def test_unknown_and_empty():
    assert categorize("SOMETHING ODD 42") == "Uncategorized"
    assert categorize("") == "Uncategorized"
    assert categorize(None) == "Uncategorized"
