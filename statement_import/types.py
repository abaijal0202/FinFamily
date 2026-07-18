"""Shared result types for every bank parser (bank-specific and generic).

Keeping these in one place means the review UI and confirm route don't need
to know which parser produced a ParsedStatement — the shape is identical
whether a dedicated bank parser or the generic fallback built it.
"""
from dataclasses import dataclass, field


@dataclass
class ParsedTransaction:
    txn_date: object
    narration: str
    withdrawal: float
    deposit: float
    balance_after: object  # float or None


@dataclass
class ParsedAccount:
    account_number: str
    account_type: str = ""
    opening_balance: object = None
    closing_balance: object = None
    debit_total: object = None
    credit_total: object = None
    transactions: list = field(default_factory=list)


@dataclass
class ParsedFixedDeposit:
    fd_number: str
    principal: float
    open_date: object
    rate: float
    current_amount: float
    maturity_date: object
    maturity_amount: float


@dataclass
class ParsedStatement:
    bank: str
    accounts: list
    fixed_deposits: list
    warnings: list
