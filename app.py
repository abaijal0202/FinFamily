import os
from datetime import datetime, date
from collections import defaultdict

from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, abort
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from flask_migrate import Migrate
from werkzeug.utils import secure_filename

from config import Config
from models import (
    db, User, Family, Asset, Goal, NetWorthSnapshot,
    ASSET_CATEGORIES, NPS_SUBASSET_CLASSES, ROLES, ROLE_OWNER, ROLE_VIEWER,
    StatementImport, ImportedAccount, ImportedTransaction, Transaction,
    ScanSender, ProcessedEmail,
    TRANSACTION_CATEGORIES, IMPORT_STATUS_PENDING, IMPORT_STATUS_CONFIRMED, IMPORT_STATUS_DISCARDED,
)
from statement_import.registry import supported_banks
import valuation
import gmail_ingest
from import_service import ingest_bank_pdf, file_sha256, find_existing_import, route_pdf
from analytics import (
    compute_alerts, compute_asset_xirr, monthly_cashflow, category_comparison,
)

login_manager = LoginManager()
migrate = Migrate()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "login"

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    # Schema is managed by Flask-Migrate: run `flask db upgrade` (the
    # start_finfamily script does this) instead of db.create_all().

    app.jinja_env.filters["inr"] = format_inr

    register_routes(app)
    return app


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def visible_assets_query(family_id, viewer):
    """Assets visible to the current viewer: everything shared, plus their own private ones."""
    q = Asset.query.filter_by(family_id=family_id)
    return [a for a in q if (not a.is_private) or (a.owner_id == viewer.id)]


def compute_net_worth(assets):
    total_assets = sum(a.current_value or 0.0 for a in assets if not a.is_liability and a.category != "INSURANCE")
    total_liabilities = sum(a.current_value or 0.0 for a in assets if a.is_liability)
    return total_assets, total_liabilities, total_assets - total_liabilities


def group_by_class(assets):
    groups = defaultdict(float)
    for a in assets:
        if a.category == "INSURANCE":
            continue
        meta = ASSET_CATEGORIES.get(a.category, {})
        sign = -1 if meta.get("liability") else 1
        groups[meta.get("group", "Other")] += sign * (a.current_value or 0.0)
    return dict(groups)


def register_routes(app):

    # ------------------------------------------------------------------ AUTH
    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone = request.form.get("phone", "").strip()
            password = request.form.get("password", "")
            family_name = request.form.get("family_name", "").strip() or f"{name}'s Family"

            if not name or not email or not password:
                flash("Name, email and password are required.", "error")
                return render_template("register.html")

            if User.query.filter_by(email=email).first():
                flash("An account with this email already exists.", "error")
                return render_template("register.html")

            family = Family(name=family_name)
            db.session.add(family)
            db.session.flush()

            user = User(
                name=name, email=email, phone=phone,
                role=ROLE_OWNER, relationship_label="Self",
                family_id=family.id,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()

            login_user(user)
            flash("Welcome to FinFamily! Your family workspace has been created.", "success")
            return redirect(url_for("dashboard"))

        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = User.query.filter_by(email=email).first()
            if user and user.check_password(password):
                login_user(user)
                return redirect(url_for("dashboard"))
            flash("Invalid email or password.", "error")
        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    # -------------------------------------------------------------- DASHBOARD
    @app.route("/dashboard")
    @login_required
    def dashboard():
        member_id = request.args.get("member", type=int)
        family = current_user.family
        all_members = User.query.filter_by(family_id=family.id).all()

        if member_id:
            viewed_member = User.query.filter_by(id=member_id, family_id=family.id).first_or_404()
            assets = [a for a in visible_assets_query(family.id, current_user) if a.owner_id == viewed_member.id]
        else:
            viewed_member = None
            assets = visible_assets_query(family.id, current_user)

        total_assets, total_liabilities, net_worth = compute_net_worth(assets)
        allocation = group_by_class(assets)

        # Snapshots are written by the valuation refresh (startup / Refresh
        # button), not on every dashboard view — see valuation.snapshot_all_families.
        trend = NetWorthSnapshot.query.filter_by(family_id=family.id).order_by(NetWorthSnapshot.snapshot_date).all()

        liquidity_buckets = {"Liquid": 0.0, "Semi-liquid": 0.0, "Locked-in / Illiquid": 0.0}
        liquidity_map = {
            "BANK": "Liquid", "MF": "Semi-liquid", "EQUITY": "Semi-liquid", "FD": "Semi-liquid",
            "PPF": "Locked-in / Illiquid", "EPF": "Locked-in / Illiquid", "NPS": "Locked-in / Illiquid",
            "GOLD": "Locked-in / Illiquid", "REALESTATE": "Locked-in / Illiquid",
        }
        for a in assets:
            if a.category in liquidity_map and not a.is_liability:
                liquidity_buckets[liquidity_map[a.category]] += a.current_value or 0.0

        return render_template(
            "dashboard.html",
            family=family, members=all_members, viewed_member=viewed_member,
            total_assets=total_assets, total_liabilities=total_liabilities, net_worth=net_worth,
            allocation=allocation, trend=trend, liquidity_buckets=liquidity_buckets,
            categories=ASSET_CATEGORIES, alerts=compute_alerts(assets),
        )

    # ------------------------------------------------------------------ FAMILY
    @app.route("/family", methods=["GET", "POST"])
    @login_required
    def family_page():
        family = current_user.family
        members = User.query.filter_by(family_id=family.id).all()

        if request.method == "POST":
            if current_user.role != ROLE_OWNER:
                flash("Only the primary user (Owner) can manage family membership.", "error")
                return redirect(url_for("family_page"))

            if len(members) >= app.config["MAX_FAMILY_SIZE"]:
                flash(f"Family group has reached the maximum of {app.config['MAX_FAMILY_SIZE']} members.", "error")
                return redirect(url_for("family_page"))

            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            relationship_label = request.form.get("relationship_label", "Family Member")
            role = request.form.get("role", ROLE_VIEWER)
            is_managed = request.form.get("is_managed_profile") == "on"

            if not name:
                flash("Name is required.", "error")
                return redirect(url_for("family_page"))

            if not is_managed:
                if not email:
                    flash("Email is required for members who will log in themselves.", "error")
                    return redirect(url_for("family_page"))
                if User.query.filter_by(email=email).first():
                    flash("That email is already registered.", "error")
                    return redirect(url_for("family_page"))

            new_member = User(
                name=name,
                email=email or f"managed-{name.lower().replace(' ', '')}-{family.id}@finfamily.local",
                relationship_label=relationship_label,
                role=role if not is_managed else ROLE_VIEWER,
                is_managed_profile=is_managed,
                family_id=family.id,
            )
            if not is_managed:
                temp_password = request.form.get("temp_password") or "ChangeMe123!"
                new_member.set_password(temp_password)
                flash(f"{name} invited. Share their temporary password to let them log in and grant consent (FR-FAM-03).", "success")
            else:
                flash(f"{name} added as a managed profile (FR-FAM-04). You control their data directly.", "success")

            db.session.add(new_member)
            db.session.commit()
            return redirect(url_for("family_page"))

        return render_template("family.html", family=family, members=members, roles=ROLES)

    @app.route("/family/remove/<int:user_id>", methods=["POST"])
    @login_required
    def remove_member(user_id):
        if current_user.role != ROLE_OWNER:
            abort(403)
        member = User.query.filter_by(id=user_id, family_id=current_user.family_id).first_or_404()
        if member.id == current_user.id:
            flash("You cannot remove yourself.", "error")
        else:
            member.is_active_member = False
            db.session.commit()
            flash(f"{member.name} removed from the consolidated family view (FR-FAM-07).", "success")
        return redirect(url_for("family_page"))

    # ------------------------------------------------------------------ ASSETS
    @app.route("/assets")
    @login_required
    def assets_list():
        family = current_user.family
        category_filter = request.args.get("category")
        assets = visible_assets_query(family.id, current_user)
        if category_filter:
            assets = [a for a in assets if a.category == category_filter]
        assets.sort(key=lambda a: (a.category, a.name))
        # XIRR per MF asset with cashflow history: {asset_id: (pct, approximate)}
        xirr_map = {}
        for a in assets:
            if a.category == "MF":
                pct, approx = compute_asset_xirr(a)
                if pct is not None:
                    xirr_map[a.id] = (pct, approx)
        return render_template("assets_list.html", assets=assets, categories=ASSET_CATEGORIES,
                                active_category=category_filter, xirr_map=xirr_map)

    @app.route("/assets/add/<category>", methods=["GET", "POST"])
    @login_required
    def add_asset(category):
        category = category.upper()
        if category not in ASSET_CATEGORIES:
            abort(404)
        if not current_user.can_edit:
            flash("Viewers cannot add assets.", "error")
            return redirect(url_for("assets_list"))

        members = User.query.filter_by(family_id=current_user.family_id).all()

        if request.method == "POST":
            asset = Asset(
                family_id=current_user.family_id,
                owner_id=request.form.get("owner_id", type=int) or current_user.id,
                category=category,
                name=request.form.get("name", "").strip(),
                institution=request.form.get("institution", "").strip(),
                current_value=_to_float(request.form.get("current_value")),
                purchase_value=_to_float(request.form.get("purchase_value")),
                principal_or_sum_assured=_to_float(request.form.get("principal_or_sum_assured")),
                interest_rate=_to_float(request.form.get("interest_rate")),
                account_number_last4=request.form.get("account_number_last4", "").strip(),
                opened_on=_to_date(request.form.get("opened_on")),
                maturity_date=_to_date(request.form.get("maturity_date")),
                emi_amount=_to_float(request.form.get("emi_amount")),
                units=_to_float(request.form.get("units")),
                avg_buy_price=_to_float(request.form.get("avg_buy_price")),
                scheme_code=(request.form.get("scheme_code") or "").strip() or None,
                isin=(request.form.get("isin") or "").strip() or None,
                folio_number=(request.form.get("folio_number") or "").strip() or None,
                ticker=(request.form.get("ticker") or "").strip() or None,
                nps_equity_pct=_to_float(request.form.get("nps_equity_pct")) or 0.0,
                nps_corp_debt_pct=_to_float(request.form.get("nps_corp_debt_pct")) or 0.0,
                nps_gov_sec_pct=_to_float(request.form.get("nps_gov_sec_pct")) or 0.0,
                nps_alt_pct=_to_float(request.form.get("nps_alt_pct")) or 0.0,
                notes=request.form.get("notes", "").strip(),
                is_private=request.form.get("is_private") == "on",
            )
            db.session.add(asset)
            db.session.commit()
            valuation.snapshot_all_families()
            flash(f"{asset.name} added.", "success")
            return redirect(url_for("assets_list"))

        return render_template("asset_form.html", category=category, meta=ASSET_CATEGORIES[category],
                                members=members, asset=None, nps_classes=NPS_SUBASSET_CLASSES)

    @app.route("/assets/<int:asset_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_asset(asset_id):
        asset = Asset.query.filter_by(id=asset_id, family_id=current_user.family_id).first_or_404()
        if asset.is_private and asset.owner_id != current_user.id:
            abort(403)
        if not current_user.can_edit:
            flash("Viewers cannot edit assets.", "error")
            return redirect(url_for("assets_list"))

        members = User.query.filter_by(family_id=current_user.family_id).all()

        if request.method == "POST":
            asset.owner_id = request.form.get("owner_id", type=int) or asset.owner_id
            asset.name = request.form.get("name", "").strip()
            asset.institution = request.form.get("institution", "").strip()
            asset.current_value = _to_float(request.form.get("current_value"))
            asset.purchase_value = _to_float(request.form.get("purchase_value"))
            asset.principal_or_sum_assured = _to_float(request.form.get("principal_or_sum_assured"))
            asset.interest_rate = _to_float(request.form.get("interest_rate"))
            asset.account_number_last4 = request.form.get("account_number_last4", "").strip()
            asset.opened_on = _to_date(request.form.get("opened_on"))
            asset.maturity_date = _to_date(request.form.get("maturity_date"))
            asset.emi_amount = _to_float(request.form.get("emi_amount"))
            asset.units = _to_float(request.form.get("units"))
            asset.avg_buy_price = _to_float(request.form.get("avg_buy_price"))
            asset.scheme_code = (request.form.get("scheme_code") or "").strip() or None
            asset.isin = (request.form.get("isin") or "").strip() or None
            asset.folio_number = (request.form.get("folio_number") or "").strip() or None
            asset.ticker = (request.form.get("ticker") or "").strip() or None
            asset.nps_equity_pct = _to_float(request.form.get("nps_equity_pct")) or 0.0
            asset.nps_corp_debt_pct = _to_float(request.form.get("nps_corp_debt_pct")) or 0.0
            asset.nps_gov_sec_pct = _to_float(request.form.get("nps_gov_sec_pct")) or 0.0
            asset.nps_alt_pct = _to_float(request.form.get("nps_alt_pct")) or 0.0
            asset.notes = request.form.get("notes", "").strip()
            asset.is_private = request.form.get("is_private") == "on"
            asset.updated_at = datetime.utcnow()
            db.session.commit()
            valuation.snapshot_all_families()
            flash(f"{asset.name} updated.", "success")
            return redirect(url_for("assets_list"))

        return render_template("asset_form.html", category=asset.category, meta=asset.meta,
                                members=members, asset=asset, nps_classes=NPS_SUBASSET_CLASSES)

    @app.route("/assets/<int:asset_id>/delete", methods=["POST"])
    @login_required
    def delete_asset(asset_id):
        asset = Asset.query.filter_by(id=asset_id, family_id=current_user.family_id).first_or_404()
        if not current_user.can_edit:
            abort(403)
        db.session.delete(asset)
        db.session.commit()
        valuation.snapshot_all_families()
        flash("Asset removed.", "success")
        return redirect(url_for("assets_list"))

    # ------------------------------------------------------------------- GOALS
    @app.route("/goals")
    @login_required
    def goals_list():
        goals = Goal.query.filter_by(family_id=current_user.family_id).order_by(Goal.target_date).all()
        return render_template("goals.html", goals=goals)

    @app.route("/goals/add", methods=["GET", "POST"])
    @login_required
    def add_goal():
        assets = visible_assets_query(current_user.family_id, current_user)
        if request.method == "POST":
            goal = Goal(
                family_id=current_user.family_id,
                name=request.form.get("name", "").strip(),
                target_amount=_to_float(request.form.get("target_amount")) or 0.0,
                target_date=_to_date(request.form.get("target_date")),
                monthly_contribution=_to_float(request.form.get("monthly_contribution")) or 0.0,
                notes=request.form.get("notes", "").strip(),
            )
            linked_ids = request.form.getlist("linked_assets")
            if linked_ids:
                goal.linked_assets = Asset.query.filter(Asset.id.in_(linked_ids)).all()
            db.session.add(goal)
            db.session.commit()
            flash(f"Goal '{goal.name}' created.", "success")
            return redirect(url_for("goals_list"))
        return render_template("goal_form.html", goal=None, assets=assets)

    @app.route("/goals/<int:goal_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_goal(goal_id):
        goal = Goal.query.filter_by(id=goal_id, family_id=current_user.family_id).first_or_404()
        assets = visible_assets_query(current_user.family_id, current_user)
        if request.method == "POST":
            goal.name = request.form.get("name", "").strip()
            goal.target_amount = _to_float(request.form.get("target_amount")) or 0.0
            goal.target_date = _to_date(request.form.get("target_date"))
            goal.monthly_contribution = _to_float(request.form.get("monthly_contribution")) or 0.0
            goal.notes = request.form.get("notes", "").strip()
            linked_ids = request.form.getlist("linked_assets")
            goal.linked_assets = Asset.query.filter(Asset.id.in_(linked_ids)).all() if linked_ids else []
            db.session.commit()
            flash(f"Goal '{goal.name}' updated.", "success")
            return redirect(url_for("goals_list"))
        return render_template("goal_form.html", goal=goal, assets=assets)

    @app.route("/goals/<int:goal_id>/delete", methods=["POST"])
    @login_required
    def delete_goal(goal_id):
        goal = Goal.query.filter_by(id=goal_id, family_id=current_user.family_id).first_or_404()
        db.session.delete(goal)
        db.session.commit()
        flash("Goal removed.", "success")
        return redirect(url_for("goals_list"))

    # --------------------------------------------------------------- TAX VIEW
    @app.route("/tax-reference")
    @login_required
    def tax_reference():
        assets = visible_assets_query(current_user.family_id, current_user)
        gains_rows = []
        interest_rows = []
        for a in assets:
            if a.category in ("MF", "EQUITY", "GOLD", "REALESTATE") and a.purchase_value is not None:
                gain = (a.current_value or 0.0) - a.purchase_value
                holding_period_note = "Long-term" if a.opened_on and (date.today() - a.opened_on).days > 365 else "Short-term / Unknown"
                gains_rows.append({
                    "name": a.name, "category": ASSET_CATEGORIES[a.category]["label"],
                    "purchase_value": a.purchase_value, "current_value": a.current_value,
                    "gain": gain, "term": holding_period_note,
                })
            if a.category in ("FD", "BANK", "PPF") and a.interest_rate:
                est_interest = (a.current_value or 0.0) * (a.interest_rate / 100.0)
                interest_rows.append({
                    "name": a.name, "category": ASSET_CATEGORIES[a.category]["label"],
                    "rate": a.interest_rate, "estimated_annual_interest": est_interest,
                })
            if a.category == "NPS":
                interest_rows.append({
                    "name": a.name, "category": "NPS contribution (deduction reference)",
                    "rate": None, "estimated_annual_interest": None,
                })
        total_gain = sum(r["gain"] for r in gains_rows)
        total_interest = sum(r["estimated_annual_interest"] for r in interest_rows if r["estimated_annual_interest"])
        return render_template("tax_reference.html", gains_rows=gains_rows, interest_rows=interest_rows,
                                total_gain=total_gain, total_interest=total_interest)

    # --------------------------------------------------------- STATEMENT IMPORT
    @app.route("/import")
    @login_required
    def import_home():
        imports = StatementImport.query.filter_by(family_id=current_user.family_id) \
            .order_by(StatementImport.uploaded_at.desc()).all()
        members = User.query.filter_by(family_id=current_user.family_id).all()
        return render_template("import_upload.html", imports=imports, known_banks=supported_banks(),
                                gmail_on=gmail_ingest.gmail_configured(),
                                valuation_status=valuation.last_run, members=members)

    @app.route("/import/upload", methods=["POST"])
    @login_required
    def import_upload():
        if not current_user.can_edit:
            flash("Viewers cannot import statements.", "error")
            return redirect(url_for("import_home"))

        file = request.files.get("statement")
        if not file or file.filename == "":
            flash("Choose a PDF statement to upload.", "error")
            return redirect(url_for("import_home"))

        filename = secure_filename(file.filename)
        if not filename.lower().endswith(".pdf"):
            flash("Only PDF statements are supported right now.", "error")
            return redirect(url_for("import_home"))

        family_dir = os.path.join(app.config["UPLOAD_FOLDER"], str(current_user.family_id))
        os.makedirs(family_dir, exist_ok=True)
        stored_name = f"{datetime.utcnow():%Y%m%d%H%M%S}_{filename}"
        stored_path = os.path.join(family_dir, stored_name)
        file.save(stored_path)

        password = (request.form.get("pdf_password") or "").strip() or None

        # Who these holdings belong to — lets an Owner/Contributor import a
        # family member's statement without logging in as them. Must be a
        # real member of this family; otherwise fall back to the uploader.
        owner_id = request.form.get("owner_id", type=int)
        if owner_id and not User.query.filter_by(id=owner_id, family_id=current_user.family_id).first():
            owner_id = None

        try:
            result = route_pdf(
                app, current_user.family_id, current_user.id, stored_path, filename,
                source="upload", passwords=[password] if password else None,
                asset_owner_id=owner_id,
            )
        except Exception as exc:
            db.session.rollback()
            hint = " If the PDF is password-protected, enter its password above and try again." \
                if "password" not in str(exc).lower() else ""
            flash(f"Could not read that statement: {exc}.{hint}", "error")
            return redirect(url_for("import_home"))

        if result["kind"] == "duplicate":
            flash(f"This exact statement was already imported — {result['message']}. "
                  f"Nothing was imported again.", "error")
            return redirect(url_for("import_home"))
        if result["kind"] == "cas":
            created, updated, _ = result["counts"]
            flash(f"Consolidated Account Statement recognized. {created} new and {updated} updated "
                  f"mutual fund holding(s) applied. Net worth updated.", "success")
            return redirect(url_for("assets_list"))
        if result["kind"] == "nps":
            created, updated, _ = result["counts"]
            flash(f"NPS statement recognized. {created} new and {updated} updated NPS holding(s) "
                  f"applied. Net worth updated.", "success")
            return redirect(url_for("assets_list"))
        if result["kind"] == "skipped":
            flash(f"That PDF didn't contain any recognizable statement data — {result['message']}.",
                  "error")
            return redirect(url_for("import_home"))

        stmt_import = result["stmt"]
        flash(f"Parsed {stmt_import.accounts_found} account(s) and {stmt_import.transactions_found} "
              f"transaction(s). Review before confirming.", "success")
        return redirect(url_for("import_review", import_id=stmt_import.id))

    @app.route("/import/check-gmail", methods=["POST"])
    @login_required
    def gmail_check():
        if not current_user.can_edit:
            abort(403)
        if not gmail_ingest.gmail_configured():
            flash("Gmail is not configured. Add GMAIL_ADDRESS and GMAIL_APP_PASSWORD to your .env file.", "error")
            return redirect(url_for("import_home"))
        try:
            summary = gmail_ingest.check_gmail(app, current_user.family_id, current_user.id)
        except Exception as exc:
            db.session.rollback()
            flash(f"Gmail check failed: {exc}", "error")
            return redirect(url_for("import_home"))

        parts = [f"{summary['checked']} new statement email(s) examined"]
        if summary["cas"]:
            parts.append(f"{len(summary['cas'])} CAS applied to your mutual funds")
        if summary["bank_imports"]:
            parts.append(f"{len(summary['bank_imports'])} bank statement(s) staged for review below")
        if summary["errors"]:
            parts.append(f"{len(summary['errors'])} error(s): " + "; ".join(summary["errors"][:3]))
        flash(". ".join(parts) + ".", "error" if summary["errors"] else "success")
        return redirect(url_for("import_home"))

    @app.route("/refresh-valuations", methods=["POST"])
    @login_required
    def refresh_valuations():
        mf, eq, errors = valuation.run_full_refresh(app)
        if errors:
            flash("Refresh finished with issues: " + "; ".join(errors), "error")
        else:
            flash(f"Valuations refreshed: {mf} mutual fund(s) and {eq} equity holding(s) updated from latest NAVs/prices.", "success")
        return redirect(request.referrer or url_for("dashboard"))

    @app.route("/import/<int:import_id>/review")
    @login_required
    def import_review(import_id):
        stmt_import = StatementImport.query.filter_by(id=import_id, family_id=current_user.family_id).first_or_404()
        assets = Asset.query.filter(
            Asset.family_id == current_user.family_id, Asset.category.in_(["BANK", "FD"])
        ).all()
        return render_template("import_review.html", stmt_import=stmt_import, assets=assets,
                                categories=TRANSACTION_CATEGORIES)

    @app.route("/import/<int:import_id>/confirm", methods=["POST"])
    @login_required
    def import_confirm(import_id):
        stmt_import = StatementImport.query.filter_by(id=import_id, family_id=current_user.family_id).first_or_404()
        if stmt_import.status != IMPORT_STATUS_PENDING:
            flash("This import was already processed.", "error")
            return redirect(url_for("import_home"))
        if not current_user.can_edit:
            abort(403)

        def resolve_asset(choice_field, name_field, balance_field, last4_field, kind, default_name):
            """Create-or-update the Asset an account panel (parsed or manually
            added) should post to, using whatever the user has in the form
            right now, which may differ from what the parser originally read."""
            choice = request.form.get(choice_field, "new")
            if choice == "skip":
                return None
            closing_balance = _to_float(request.form.get(balance_field))
            last4_raw = (request.form.get(last4_field) or "").strip()
            last4 = last4_raw[-4:] if last4_raw else None
            name = (request.form.get(name_field) or "").strip() or default_name

            if choice == "new":
                asset = Asset(
                    family_id=current_user.family_id,
                    owner_id=stmt_import.asset_owner_id or stmt_import.uploaded_by_id,
                    category=kind if kind in ASSET_CATEGORIES else "BANK",
                    name=name, institution=stmt_import.bank,
                    current_value=closing_balance if closing_balance is not None else 0.0,
                    account_number_last4=last4,
                )
                db.session.add(asset)
                db.session.flush()
                return asset

            asset = Asset.query.filter_by(id=int(choice), family_id=current_user.family_id).first()
            if not asset:
                return None
            if closing_balance is not None:
                asset.current_value = closing_balance
            if last4:
                asset.account_number_last4 = asset.account_number_last4 or last4
            asset.updated_at = datetime.utcnow()
            return asset

        def add_new_rows(prefix, asset):
            """Read `{prefix}_date_{n}`-style fields appended client-side via
            the "+ Add transaction" button, for n = 0, 1, 2... until one is
            missing from the form."""
            added = 0
            idx = 0
            while f"{prefix}_date_{idx}" in request.form:
                raw_date = (request.form.get(f"{prefix}_date_{idx}") or "").strip()
                txn_date = _to_date(raw_date) if raw_date else None
                if txn_date:
                    db.session.add(Transaction(
                        family_id=current_user.family_id, asset_id=asset.id,
                        txn_date=txn_date,
                        narration=(request.form.get(f"{prefix}_narration_{idx}") or "").strip(),
                        withdrawal=_to_float(request.form.get(f"{prefix}_withdrawal_{idx}")) or 0.0,
                        deposit=_to_float(request.form.get(f"{prefix}_deposit_{idx}")) or 0.0,
                        balance_after=_to_float(request.form.get(f"{prefix}_balance_{idx}")),
                        category=request.form.get(f"{prefix}_category_{idx}", "Uncategorized"),
                        source_import_id=stmt_import.id,
                    ))
                    added += 1
                idx += 1
            return added

        txns_added = 0

        # 1. Accounts the parser found (possibly edited on the review screen).
        for acc in stmt_import.accounts:
            asset = resolve_asset(
                f"asset_choice_{acc.id}", f"asset_name_{acc.id}",
                f"closing_balance_{acc.id}", f"last4_{acc.id}",
                acc.account_kind, acc.suggested_name,
            )
            if asset is None:
                continue
            acc.matched_asset_id = asset.id

            for t in acc.transactions:
                if request.form.get(f"include_txn_{t.id}") != "on" or t.is_duplicate:
                    continue
                raw_date = (request.form.get(f"txn_date_{t.id}") or "").strip()
                txn_date = (_to_date(raw_date) if raw_date else None) or t.txn_date
                withdrawal = _to_float(request.form.get(f"txn_withdrawal_{t.id}"))
                deposit = _to_float(request.form.get(f"txn_deposit_{t.id}"))
                balance_after = _to_float(request.form.get(f"txn_balance_{t.id}"))
                db.session.add(Transaction(
                    family_id=current_user.family_id, asset_id=asset.id,
                    txn_date=txn_date,
                    narration=(request.form.get(f"txn_narration_{t.id}") or t.narration or "").strip(),
                    withdrawal=withdrawal if withdrawal is not None else t.withdrawal,
                    deposit=deposit if deposit is not None else t.deposit,
                    balance_after=balance_after if balance_after is not None else t.balance_after,
                    category=request.form.get(f"category_{t.id}", t.category),
                    source_import_id=stmt_import.id,
                ))
                txns_added += 1

            txns_added += add_new_rows(f"new_txn_{acc.id}", asset)

        # 2. Accounts the parser missed entirely, added by hand on the review screen.
        manual_count = request.form.get("manual_account_count", type=int) or 0
        for i in range(manual_count):
            name = (request.form.get(f"manual_asset_name_{i}") or "").strip()
            if not name:
                continue
            asset = resolve_asset(
                f"manual_asset_choice_{i}", f"manual_asset_name_{i}",
                f"manual_closing_balance_{i}", f"manual_last4_{i}",
                request.form.get(f"manual_account_kind_{i}", "BANK"), name,
            )
            if asset is None:
                continue
            txns_added += add_new_rows(f"manual_txn_{i}", asset)

        stmt_import.status = IMPORT_STATUS_CONFIRMED
        stmt_import.confirmed_at = datetime.utcnow()
        db.session.commit()
        valuation.snapshot_all_families()
        flash(f"Import confirmed: {txns_added} transaction(s) added to your ledger.", "success")
        return redirect(url_for("assets_list"))

    @app.route("/import/<int:import_id>/discard", methods=["POST"])
    @login_required
    def import_discard(import_id):
        stmt_import = StatementImport.query.filter_by(id=import_id, family_id=current_user.family_id).first_or_404()
        if stmt_import.status == IMPORT_STATUS_PENDING:
            stmt_import.status = IMPORT_STATUS_DISCARDED
            db.session.commit()
            flash("Import discarded.", "success")
        return redirect(url_for("import_home"))

    @app.route("/import/<int:import_id>/delete", methods=["POST"])
    @login_required
    def import_delete(import_id):
        """Permanently remove a pending or discarded import from history
        (record + its parsed accounts/transactions + the stored PDF).
        Confirmed imports can't be deleted — they're the audit trail for
        ledger entries already added to your assets."""
        stmt_import = StatementImport.query.filter_by(id=import_id, family_id=current_user.family_id).first_or_404()
        if not current_user.can_edit:
            abort(403)
        if stmt_import.status == IMPORT_STATUS_CONFIRMED:
            flash("Confirmed imports can't be deleted — their transactions are already in your ledger.", "error")
            return redirect(url_for("import_home"))
        stored = stmt_import.stored_path
        db.session.delete(stmt_import)  # cascades to ImportedAccount/ImportedTransaction
        db.session.commit()
        if stored and os.path.exists(stored):
            try:
                os.remove(stored)
            except OSError:
                pass  # record is gone; a leftover file is harmless
        flash("Import removed from history.", "success")
        return redirect(url_for("import_home"))

    @app.route("/import/delete-selected", methods=["POST"])
    @login_required
    def import_delete_selected():
        """Bulk-remove pending/discarded imports selected via checkboxes.
        Confirmed imports are skipped (their transactions are in the ledger)."""
        if not current_user.can_edit:
            abort(403)
        ids = request.form.getlist("import_ids", type=int)
        if not ids:
            flash("No imports selected.", "error")
            return redirect(url_for("import_home"))
        rows = StatementImport.query.filter(
            StatementImport.id.in_(ids),
            StatementImport.family_id == current_user.family_id,
            StatementImport.status != IMPORT_STATUS_CONFIRMED,
        ).all()
        deleted = 0
        for imp in rows:
            stored = imp.stored_path
            db.session.delete(imp)
            deleted += 1
            if stored and os.path.exists(stored):
                try:
                    os.remove(stored)
                except OSError:
                    pass
        db.session.commit()
        skipped = len(ids) - deleted
        msg = f"Removed {deleted} import(s) from history."
        if skipped:
            msg += f" {skipped} confirmed import(s) were skipped — their transactions are in your ledger."
        flash(msg, "success")
        return redirect(url_for("import_home"))

    # --------------------------------------------------------- IMPORT SETTINGS
    @app.route("/import/settings")
    @login_required
    def import_settings():
        senders = ScanSender.query.filter_by(family_id=current_user.family_id) \
            .order_by(ScanSender.created_at).all()
        recent_scans = ProcessedEmail.query.filter_by(family_id=current_user.family_id) \
            .order_by(ProcessedEmail.processed_at.desc()).limit(50).all()
        return render_template("import_settings.html", senders=senders,
                                recent_scans=recent_scans,
                                default_senders=gmail_ingest.DEFAULT_SENDERS,
                                gmail_on=gmail_ingest.gmail_configured())

    @app.route("/import/settings/senders/add", methods=["POST"])
    @login_required
    def scan_sender_add():
        if current_user.role != ROLE_OWNER:
            abort(403)
        email_frag = (request.form.get("email") or "").strip().lower()
        if not email_frag:
            flash("Enter an email address or domain (e.g. donotreply@camsonline.com or camsonline.com).", "error")
            return redirect(url_for("import_settings"))
        db.session.add(ScanSender(
            family_id=current_user.family_id, email=email_frag,
            attachment_password=(request.form.get("attachment_password") or "").strip() or None,
            notes=(request.form.get("notes") or "").strip() or None,
        ))
        db.session.commit()
        flash(f"'{email_frag}' will be scanned on the next Gmail check.", "success")
        return redirect(url_for("import_settings"))

    @app.route("/import/settings/senders/<int:sender_id>/delete", methods=["POST"])
    @login_required
    def scan_sender_delete(sender_id):
        if current_user.role != ROLE_OWNER:
            abort(403)
        sender = ScanSender.query.filter_by(id=sender_id, family_id=current_user.family_id).first_or_404()
        db.session.delete(sender)
        db.session.commit()
        flash("Sender removed.", "success")
        return redirect(url_for("import_settings"))

    @app.route("/import/settings/scan-history/clear", methods=["POST"])
    @login_required
    def scan_history_clear():
        """Forget which emails were already examined, so the next Gmail check
        re-scans everything in the search window — the way to retry an email
        that previously failed (e.g. CAS with a wrong password)."""
        if current_user.role != ROLE_OWNER:
            abort(403)
        deleted = ProcessedEmail.query.filter_by(family_id=current_user.family_id).delete()
        db.session.commit()
        flash(f"Scan history cleared ({deleted} record(s)). The next Gmail check will re-examine "
              f"all statement emails in the search window; already-imported PDFs are still "
              f"skipped by the duplicate guard.", "success")
        return redirect(url_for("import_settings"))

    # ------------------------------------------------------------- TRANSACTIONS
    @app.route("/transactions")
    @login_required
    def transactions_list():
        family = current_user.family
        asset_id = request.args.get("asset", type=int)
        assets = visible_assets_query(family.id, current_user)
        asset_ids = [a.id for a in assets] or [-1]
        q = Transaction.query.filter(Transaction.family_id == family.id, Transaction.asset_id.in_(asset_ids))
        if asset_id:
            q = q.filter(Transaction.asset_id == asset_id)
        txns = q.order_by(Transaction.txn_date.desc(), Transaction.id.desc()).limit(300).all()
        return render_template("transactions.html", transactions=txns, assets=assets, active_asset=asset_id)

    # --------------------------------------------------------------- CASH FLOW
    @app.route("/cashflow")
    @login_required
    def cashflow():
        family = current_user.family
        assets = visible_assets_query(family.id, current_user)
        asset_ids = [a.id for a in assets] or [-1]
        txns = Transaction.query.filter(
            Transaction.family_id == family.id,
            Transaction.asset_id.in_(asset_ids),
        ).all()
        months = monthly_cashflow(txns, months=12)
        cat_rows, this_label, prev_label = category_comparison(txns)
        return render_template("cashflow.html", months=months, cat_rows=cat_rows,
                                this_label=this_label, prev_label=prev_label,
                                has_data=any(m["income"] or m["expense"] for m in months))

    # -------------------------------------------------------------- JSON API
    @app.route("/api/dashboard_data")
    @login_required
    def api_dashboard_data():
        assets = visible_assets_query(current_user.family_id, current_user)
        allocation = group_by_class(assets)
        trend = NetWorthSnapshot.query.filter_by(family_id=current_user.family_id).order_by(NetWorthSnapshot.snapshot_date).all()
        return jsonify({
            "allocation": allocation,
            "trend": [{"date": t.snapshot_date.isoformat(), "net_worth": t.net_worth} for t in trend],
        })


def _to_float(val):
    if val in (None, ""):
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _to_date(val):
    if not val:
        return None
    try:
        return datetime.strptime(val, "%Y-%m-%d").date()
    except ValueError:
        return None


def format_inr(value, decimals=0):
    """Format a number using the Indian digit-grouping convention
    (last 3 digits, then groups of 2: 12,34,567 not 1,234,567). Used as the
    `inr` Jinja filter everywhere a rupee amount is shown."""
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0.0
    negative = value < 0
    value = abs(value)

    if decimals:
        int_part, _, frac_part = f"{value:.{decimals}f}".partition(".")
    else:
        int_part, frac_part = str(int(round(value))), None

    if len(int_part) > 3:
        last3, rest = int_part[-3:], int_part[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        int_part = ",".join(groups) + "," + last3

    result = f"{int_part}.{frac_part}" if frac_part is not None else int_part
    return f"-{result}" if negative else result


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
