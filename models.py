from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

# ---------------------------------------------------------------------------
# Asset class taxonomy (Section 6.2 / Appendix B of the BRD)
# ---------------------------------------------------------------------------
ASSET_CATEGORIES = {
    "BANK": {"label": "Bank Account", "group": "Cash & Bank", "liability": False},
    "FD": {"label": "Fixed / Recurring Deposit", "group": "Fixed Income", "liability": False},
    "MF": {"label": "Mutual Fund", "group": "Market-linked Investments", "liability": False},
    "EQUITY": {"label": "Direct Equity / F&O", "group": "Market-linked Investments", "liability": False},
    "PPF": {"label": "PPF", "group": "Retirement - Government-backed", "liability": False},
    "EPF": {"label": "EPF", "group": "Retirement - Government-backed", "liability": False},
    "NPS": {"label": "NPS", "group": "Retirement - Market-linked", "liability": False},
    "GOLD": {"label": "Gold", "group": "Physical & Other Assets", "liability": False},
    "REALESTATE": {"label": "Real Estate", "group": "Physical & Other Assets", "liability": False},
    "LOAN": {"label": "Loan / Liability", "group": "Liabilities", "liability": True},
    "INSURANCE": {"label": "Insurance Policy", "group": "Insurance (not counted in net worth)", "liability": False},
}

NPS_SUBASSET_CLASSES = ["E", "C", "G", "A"]  # Equity, Corporate Debt, Govt Securities, Alternative

ROLE_OWNER = "Owner"
ROLE_CONTRIBUTOR = "Contributor"
ROLE_VIEWER = "Viewer"
ROLES = [ROLE_OWNER, ROLE_CONTRIBUTOR, ROLE_VIEWER]


class Family(db.Model):
    __tablename__ = "families"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    members = db.relationship("User", backref="family", lazy=True)
    assets = db.relationship("Asset", backref="family", lazy=True)
    goals = db.relationship("Goal", backref="family", lazy=True)


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False)
    phone = db.Column(db.String(20))
    password_hash = db.Column(db.String(255))
    role = db.Column(db.String(20), default=ROLE_OWNER)  # Owner / Contributor / Viewer
    relationship_label = db.Column(db.String(60), default="Self")  # Spouse, Child, Parent, Self
    is_managed_profile = db.Column(db.Boolean, default=False)  # FR-FAM-04: non-authenticating dependent
    is_active_member = db.Column(db.Boolean, default=True)
    family_id = db.Column(db.Integer, db.ForeignKey("families.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    assets_owned = db.relationship("Asset", backref="owner", lazy=True, foreign_keys="Asset.owner_id")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    @property
    def can_edit(self):
        return self.role in (ROLE_OWNER, ROLE_CONTRIBUTOR)


class Asset(db.Model):
    """Generic holding record covering every asset class in the BRD (Sec 7)."""
    __tablename__ = "assets"
    id = db.Column(db.Integer, primary_key=True)
    family_id = db.Column(db.Integer, db.ForeignKey("families.id"), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    category = db.Column(db.String(20), nullable=False)  # key in ASSET_CATEGORIES
    name = db.Column(db.String(160), nullable=False)  # e.g. "HDFC Savings", "Axis Bluechip Fund"
    institution = db.Column(db.String(160))  # bank / AMC / broker / insurer name

    current_value = db.Column(db.Float, default=0.0)  # for LOAN this is outstanding balance
    purchase_value = db.Column(db.Float)  # cost basis, used for gains estimate
    principal_or_sum_assured = db.Column(db.Float)  # FD principal / insurance sum assured
    interest_rate = db.Column(db.Float)  # annual %, used for PPF/FD projections
    account_number_last4 = db.Column(db.String(10))
    opened_on = db.Column(db.Date)
    maturity_date = db.Column(db.Date)

    # NPS asset-class split (percent), stored individually for simplicity
    nps_equity_pct = db.Column(db.Float, default=0.0)
    nps_corp_debt_pct = db.Column(db.Float, default=0.0)
    nps_gov_sec_pct = db.Column(db.Float, default=0.0)
    nps_alt_pct = db.Column(db.Float, default=0.0)

    # Loan-specific
    emi_amount = db.Column(db.Float)

    notes = db.Column(db.Text)
    is_private = db.Column(db.Boolean, default=False)  # FR-FAM-05
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def meta(self):
        return ASSET_CATEGORIES.get(self.category, {})

    @property
    def is_liability(self):
        return self.meta.get("liability", False)

    @property
    def net_worth_contribution(self):
        """Signed contribution to net worth. Insurance is excluded per Appendix B."""
        if self.category == "INSURANCE":
            return 0.0
        if self.is_liability:
            return -(self.current_value or 0.0)
        return self.current_value or 0.0

    @property
    def unrealized_gain(self):
        if self.purchase_value is None or self.category not in ("MF", "EQUITY", "GOLD", "REALESTATE"):
            return None
        return (self.current_value or 0.0) - self.purchase_value


goal_assets = db.Table(
    "goal_assets",
    db.Column("goal_id", db.Integer, db.ForeignKey("goals.id"), primary_key=True),
    db.Column("asset_id", db.Integer, db.ForeignKey("assets.id"), primary_key=True),
)


class Goal(db.Model):
    __tablename__ = "goals"
    id = db.Column(db.Integer, primary_key=True)
    family_id = db.Column(db.Integer, db.ForeignKey("families.id"), nullable=False)
    name = db.Column(db.String(160), nullable=False)
    target_amount = db.Column(db.Float, nullable=False)
    target_date = db.Column(db.Date)
    monthly_contribution = db.Column(db.Float, default=0.0)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    linked_assets = db.relationship("Asset", secondary=goal_assets, backref="linked_goals")

    @property
    def current_corpus(self):
        return sum(a.current_value or 0.0 for a in self.linked_assets)

    @property
    def progress_pct(self):
        if not self.target_amount:
            return 0
        return min(100, round(100 * self.current_corpus / self.target_amount, 1))

    @property
    def shortfall(self):
        return max(0.0, self.target_amount - self.current_corpus)

    @property
    def months_remaining(self):
        if not self.target_date:
            return None
        today = date.today()
        return max(0, (self.target_date.year - today.year) * 12 + (self.target_date.month - today.month))

    @property
    def projected_shortfall_monthly_sip(self):
        """Suggested monthly SIP top-up to close the shortfall by target date."""
        months = self.months_remaining
        if not months:
            return None
        if months <= 0:
            return self.shortfall
        return round(self.shortfall / months, 2)


class NetWorthSnapshot(db.Model):
    """Daily consolidated snapshot used for the trend chart (Sec 10.1)."""
    __tablename__ = "networth_snapshots"
    id = db.Column(db.Integer, primary_key=True)
    family_id = db.Column(db.Integer, db.ForeignKey("families.id"), nullable=False)
    snapshot_date = db.Column(db.Date, default=date.today)
    total_assets = db.Column(db.Float)
    total_liabilities = db.Column(db.Float)
    net_worth = db.Column(db.Float)


# ---------------------------------------------------------------------------
# Bank statement import (BRD Sec 8 fallback path: "statement upload/OCR" for
# banks not yet live on Account Aggregator) + transaction ledger (Sec 6.4)
# ---------------------------------------------------------------------------
TRANSACTION_CATEGORIES = [
    "Salary", "Interest Income", "Dividend / Auto-Credit", "Transfer In", "Transfer Out",
    "Internal Transfer", "Bills & Utilities", "Food & Groceries", "Shopping / POS",
    "Cash Withdrawal", "UPI Transfer", "Uncategorized",
]

IMPORT_STATUS_PENDING = "pending_review"
IMPORT_STATUS_CONFIRMED = "confirmed"
IMPORT_STATUS_DISCARDED = "discarded"


class StatementImport(db.Model):
    """Audit trail for every bank-statement PDF uploaded (NFR-SEC-07)."""
    __tablename__ = "statement_imports"
    id = db.Column(db.Integer, primary_key=True)
    family_id = db.Column(db.Integer, db.ForeignKey("families.id"), nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    bank = db.Column(db.String(40))
    original_filename = db.Column(db.String(255))
    stored_path = db.Column(db.String(500))
    status = db.Column(db.String(20), default=IMPORT_STATUS_PENDING)
    accounts_found = db.Column(db.Integer, default=0)
    transactions_found = db.Column(db.Integer, default=0)
    warnings = db.Column(db.Text)  # newline-separated parser warnings, shown on review page
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    confirmed_at = db.Column(db.DateTime)

    uploaded_by = db.relationship("User")
    accounts = db.relationship("ImportedAccount", backref="statement_import",
                                lazy=True, cascade="all, delete-orphan")

    @property
    def warning_list(self):
        return [w for w in (self.warnings or "").split("\n") if w]


class ImportedAccount(db.Model):
    """One bank/FD account as parsed from a StatementImport, pending confirmation."""
    __tablename__ = "imported_accounts"
    id = db.Column(db.Integer, primary_key=True)
    statement_import_id = db.Column(db.Integer, db.ForeignKey("statement_imports.id"), nullable=False)
    account_kind = db.Column(db.String(10), default="BANK")  # BANK or FD
    account_number = db.Column(db.String(30))
    account_number_last4 = db.Column(db.String(10))
    account_type = db.Column(db.String(120))
    opening_balance = db.Column(db.Float)
    closing_balance = db.Column(db.Float)
    debit_total = db.Column(db.Float)
    credit_total = db.Column(db.Float)
    interest_rate = db.Column(db.Float)          # FD only
    maturity_date = db.Column(db.Date)            # FD only
    matched_asset_id = db.Column(db.Integer, db.ForeignKey("assets.id"))
    suggested_name = db.Column(db.String(160))

    matched_asset = db.relationship("Asset")
    transactions = db.relationship("ImportedTransaction", backref="imported_account",
                                    lazy=True, cascade="all, delete-orphan")


class ImportedTransaction(db.Model):
    """One transaction row parsed from a statement, pending confirmation."""
    __tablename__ = "imported_transactions"
    id = db.Column(db.Integer, primary_key=True)
    imported_account_id = db.Column(db.Integer, db.ForeignKey("imported_accounts.id"), nullable=False)
    txn_date = db.Column(db.Date, nullable=False)
    narration = db.Column(db.String(500))
    withdrawal = db.Column(db.Float, default=0.0)
    deposit = db.Column(db.Float, default=0.0)
    balance_after = db.Column(db.Float)
    category = db.Column(db.String(40))
    is_duplicate = db.Column(db.Boolean, default=False)
    include = db.Column(db.Boolean, default=True)


class Transaction(db.Model):
    """Confirmed, persisted ledger entry linked to a BANK/FD asset (BRD Sec 6.4)."""
    __tablename__ = "transactions"
    id = db.Column(db.Integer, primary_key=True)
    family_id = db.Column(db.Integer, db.ForeignKey("families.id"), nullable=False)
    asset_id = db.Column(db.Integer, db.ForeignKey("assets.id"), nullable=False)
    txn_date = db.Column(db.Date, nullable=False)
    narration = db.Column(db.String(500))
    withdrawal = db.Column(db.Float, default=0.0)
    deposit = db.Column(db.Float, default=0.0)
    balance_after = db.Column(db.Float)
    category = db.Column(db.String(40))
    source_import_id = db.Column(db.Integer, db.ForeignKey("statement_imports.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    asset = db.relationship("Asset", backref="transactions")

    @property
    def amount(self):
        """Signed amount: positive = credit, negative = debit."""
        return (self.deposit or 0.0) - (self.withdrawal or 0.0)
