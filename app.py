import os
import re
from datetime import datetime, timezone
from uuid import uuid4
from functools import wraps
from urllib.parse import urlencode

import truststore
truststore.inject_into_ssl()

import requests
from dotenv import load_dotenv
from flask import Flask, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_wtf import CSRFProtect
from PIL import Image, UnidentifiedImageError
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy import text
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from models import COMPANY_STATUSES, USER_ROLES, AuditLog, Company, CompanyChange, CompanyManager, User, db


load_dotenv()

DISCORD_API_BASE = "https://discord.com/api"
DISCORD_AUTHORIZE_URL = f"{DISCORD_API_BASE}/oauth2/authorize"
DISCORD_TOKEN_URL = f"{DISCORD_API_BASE}/oauth2/token"
DISCORD_USER_URL = f"{DISCORD_API_BASE}/users/@me"
DISCORD_DM_URL = f"{DISCORD_API_BASE}/users/@me/channels"
ALLOWED_LOGO_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
STATUS_LABELS = {
    "pending": "Wartet auf Freigabe",
    "active": "Aktiv",
    "inactive": "Inaktiv",
    "dissolved": "Aufgelöst",
    "rejected": "Abgelehnt",
}
ROLE_LABELS = {
    "viewer": "Zuschauer",
    "member": "Mitglied",
    "owner": "Eigentümer",
    "admin": "Admin",
}


def create_app():
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///register.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
    app.config["LOGO_UPLOAD_FOLDER"] = os.path.join(app.static_folder, "uploads", "logos")
    os.makedirs(app.config["LOGO_UPLOAD_FOLDER"], exist_ok=True)

    db.init_app(app)
    CSRFProtect(app)
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=["200 per hour"],
        storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    )
    app.extensions["limiter"] = limiter

    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.login_message = "Bitte melde dich mit Discord an."
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    with app.app_context():
        db.create_all()
        ensure_schema()
        sync_admin_roles()

    register_routes(app)
    register_security_headers(app)
    return app


def register_routes(app):
    @app.context_processor
    def inject_constants():
        return {
            "company_statuses": COMPANY_STATUSES,
            "user_roles": USER_ROLES,
            "status_labels": STATUS_LABELS,
            "role_labels": ROLE_LABELS,
        }

    @app.route("/")
    def index():
        search_query = request.args.get("q", "").strip()
        status_filter = request.args.get("status", "").strip()
        industry_filter = request.args.get("industry", "").strip()
        district_filter = request.args.get("district", "").strip()
        page = request.args.get("page", 1, type=int)
        companies_query = Company.query.filter(Company.deleted_at.is_(None)).join(User)
        if search_query:
            pattern = f"%{search_query}%"
            companies_query = companies_query.filter(
                db.or_(
                    Company.name.ilike(pattern),
                    Company.short_name.ilike(pattern),
                    Company.description.ilike(pattern),
                    Company.industry.ilike(pattern),
                    Company.headquarters.ilike(pattern),
                    Company.district.ilike(pattern),
                    User.username.ilike(pattern),
                )
            )
        if status_filter:
            companies_query = companies_query.filter(Company.status == status_filter)
        if industry_filter:
            companies_query = companies_query.filter(Company.industry == industry_filter)
        if district_filter:
            companies_query = companies_query.filter(Company.district == district_filter)

        pagination = companies_query.order_by(Company.created_at.desc()).paginate(page=page, per_page=6, error_out=False)
        industries = [row[0] for row in db.session.query(Company.industry).filter(Company.deleted_at.is_(None), Company.industry != "").distinct().order_by(Company.industry).all()]
        districts = [row[0] for row in db.session.query(Company.district).filter(Company.deleted_at.is_(None), Company.district != "").distinct().order_by(Company.district).all()]
        return render_template(
            "index.html",
            companies=pagination.items,
            pagination=pagination,
            search_query=search_query,
            status_filter=status_filter,
            industry_filter=industry_filter,
            district_filter=district_filter,
            industries=industries,
            districts=districts,
        )

    @app.route("/me")
    @login_required
    def my_companies():
        companies = (
            Company.query.outerjoin(CompanyManager)
            .filter(Company.deleted_at.is_(None))
            .filter(db.or_(Company.owner_id == current_user.id, CompanyManager.user_id == current_user.id))
            .order_by(Company.created_at.desc())
            .all()
        )
        return render_template("my_companies.html", companies=companies)

    @app.route("/company/<int:company_id>")
    def company_detail(company_id):
        company = Company.query.filter_by(id=company_id, deleted_at=None).first_or_404()
        return render_template("company_detail.html", company=company)

    @app.route("/company/new", methods=["GET", "POST"])
    @app.extensions["limiter"].limit("20 per hour")
    @login_required
    def company_new():
        if current_user.role == "viewer":
            flash("Du brauchst die Rolle Mitglied, um Firmen beantragen zu können.", "error")
            return redirect(url_for("index"))

        suggestions = get_company_suggestions()
        if request.method == "POST":
            company = Company(
                name=request.form.get("name", "").strip(),
                short_name=request.form.get("short_name", "").strip().upper(),
                description=request.form.get("description", "").strip(),
                industry=request.form.get("industry", "").strip(),
                status="pending",
                headquarters=request.form.get("headquarters", "").strip(),
                district=request.form.get("district", "").strip(),
                owner_id=current_user.id,
            )
            error = validate_company(company)
            if error:
                flash(error, "error")
                return render_template("company_form.html", company=company, title="Firma beantragen", users=[], suggestions=suggestions)

            db.session.add(company)
            if current_user.role == "member":
                current_user.role = "owner"
            db.session.flush()
            company.register_id = generate_register_id(company.id)
            logo_error = save_company_logo(company, request.files.get("logo"))
            if logo_error:
                db.session.rollback()
                flash(logo_error, "error")
                return render_template("company_form.html", company=company, title="Firma beantragen", users=[], suggestions=suggestions)
            add_change(company, current_user, "created", "Firma wurde beantragt.")
            add_audit(current_user, "company_created", "company", company.id, f"Firma {company.name} wurde beantragt.")
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("Dieses Kürzel ist bereits vergeben.", "error")
                return render_template("company_form.html", company=company, title="Firma beantragen", users=[], suggestions=suggestions)

            notify_company_submitted(company, current_user)
            send_admin_channel_message(f"Neue Firma eingereicht: **{company.name}** ({company.register_id}) von {current_user.username}.")
            flash("Firma wurde beantragt und wartet auf Freigabe.", "success")
            return redirect(url_for("company_detail", company_id=company.id))

        return render_template("company_form.html", company=None, title="Firma beantragen", users=[], suggestions=suggestions)

    @app.route("/company/<int:company_id>/edit", methods=["GET", "POST"])
    @app.extensions["limiter"].limit("60 per hour")
    @login_required
    def company_edit(company_id):
        company = Company.query.filter_by(id=company_id, deleted_at=None).first_or_404()
        if not current_user.can_edit_company(company):
            abort(403)

        can_manage_coowners = current_user.is_admin or company.owner_id == current_user.id
        users = User.query.order_by(User.username.asc()).all() if can_manage_coowners else []
        available_parent_companies = (
            Company.query.filter(Company.deleted_at.is_(None), Company.id != company.id)
            .order_by(Company.name.asc())
            .all()
        )
        suggestions = get_company_suggestions()
        if request.method == "POST":
            old_status = company.status
            old_owner_id = company.owner_id
            status_reason = request.form.get("status_reason", "").strip()
            company.name = request.form.get("name", "").strip()
            company.short_name = request.form.get("short_name", "").strip().upper()
            company.description = request.form.get("description", "").strip()
            company.industry = request.form.get("industry", "").strip()
            company.headquarters = request.form.get("headquarters", "").strip()
            company.district = request.form.get("district", "").strip()
            requested_parent_id = request.form.get("parent_company_id", type=int)
            company.parent_company_id = requested_parent_id if requested_parent_id else None
            if company.parent_company_id == company.id:
                flash("Eine Firma kann nicht ihr eigenes Mutterunternehmen sein.", "error")
                return render_template(
                    "company_form.html",
                    company=company,
                    title="Firma bearbeiten",
                    users=users,
                    suggestions=suggestions,
                    can_manage_coowners=can_manage_coowners,
                    available_parent_companies=available_parent_companies,
                )
            if request.form.get("remove_logo") == "1":
                company.logo_filename = None
            logo_error = save_company_logo(company, request.files.get("logo"))
            if logo_error:
                flash(logo_error, "error")
                company.status = old_status
                company.owner_id = old_owner_id
                return render_template("company_form.html", company=company, title="Firma bearbeiten", users=users, suggestions=suggestions, can_manage_coowners=can_manage_coowners, available_parent_companies=available_parent_companies)
            if current_user.is_admin:
                requested_status = request.form.get("status", company.status)
                if requested_status in COMPANY_STATUSES:
                    company.status = requested_status
                if old_status != company.status and company.status == "rejected" and not status_reason:
                    flash("Bitte gib beim Ablehnen einen Grund an.", "error")
                    company.status = old_status
                    return render_template("company_form.html", company=company, title="Firma bearbeiten", users=users, suggestions=suggestions, can_manage_coowners=can_manage_coowners, available_parent_companies=available_parent_companies)
                requested_owner_id = request.form.get("owner_id", type=int)
                new_owner = db.session.get(User, requested_owner_id) if requested_owner_id else None
                if not new_owner:
                    flash("Ungültiger Eigentümer.", "error")
                    company.status = old_status
                    return render_template("company_form.html", company=company, title="Firma bearbeiten", users=users, suggestions=suggestions, can_manage_coowners=can_manage_coowners, available_parent_companies=available_parent_companies)
                company.owner_id = new_owner.id
                promote_company_actor(new_owner)

            if can_manage_coowners:
                manager_ids = {int(value) for value in request.form.getlist("manager_ids") if value.isdigit()}
                CompanyManager.query.filter_by(company_id=company.id).delete()
                for manager_id in manager_ids:
                    manager = db.session.get(User, manager_id)
                    if manager_id != company.owner_id and manager:
                        promote_company_actor(manager)
                        db.session.add(CompanyManager(company_id=company.id, user_id=manager_id))

            error = validate_company(company)
            if error:
                flash(error, "error")
                company.status = old_status
                company.owner_id = old_owner_id
                return render_template("company_form.html", company=company, title="Firma bearbeiten", users=users, suggestions=suggestions, can_manage_coowners=can_manage_coowners, available_parent_companies=available_parent_companies)

            add_change(company, current_user, "updated", "Firmendaten wurden bearbeitet.")
            if old_status != company.status:
                description = f"Status geändert: {old_status} -> {company.status}"
                if status_reason:
                    description = f"{description}. Grund: {status_reason}"
                add_change(company, current_user, "status_changed", description)
                add_audit(current_user, "company_status_changed", "company", company.id, description)
            if old_owner_id != company.owner_id:
                old_owner = db.session.get(User, old_owner_id)
                old_owner_name = old_owner.username if old_owner else "Unbekannt"
                add_change(company, current_user, "owner_changed", f"Eigentümer geändert: {old_owner_name} -> {company.owner.username}")
                add_audit(current_user, "company_owner_changed", "company", company.id, f"Owner geändert: {old_owner_name} -> {company.owner.username}")
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("Dieses Kürzel ist bereits vergeben.", "error")
                return render_template("company_form.html", company=company, title="Firma bearbeiten", users=users, suggestions=suggestions, can_manage_coowners=can_manage_coowners, available_parent_companies=available_parent_companies)

            if old_status != company.status:
                notify_company_status_changed(company, old_status, company.status, status_reason)
            flash("Firma wurde gespeichert.", "success")
            return redirect(url_for("company_detail", company_id=company.id))

        return render_template("company_form.html", company=company, title="Firma bearbeiten", users=users, suggestions=suggestions, can_manage_coowners=can_manage_coowners, available_parent_companies=available_parent_companies)

    @app.route("/admin")
    @login_required
    @admin_required
    def admin_dashboard():
        companies = Company.query.filter(Company.deleted_at.is_(None)).order_by(Company.created_at.desc()).all()
        users = User.query.order_by(User.created_at.desc()).all()
        stats = {
            "pending": Company.query.filter_by(status="pending", deleted_at=None).count(),
            "active": Company.query.filter_by(status="active", deleted_at=None).count(),
            "rejected": Company.query.filter_by(status="rejected", deleted_at=None).count(),
            "users": User.query.count(),
        }
        audit_logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(12).all()
        return render_template("admin.html", companies=companies, users=users, stats=stats, audit_logs=audit_logs, pending_only=False)

    @app.route("/admin/pending")
    @login_required
    @admin_required
    def admin_pending():
        companies = Company.query.filter_by(status="pending", deleted_at=None).order_by(Company.created_at.desc()).all()
        users = User.query.order_by(User.created_at.desc()).all()
        stats = {
            "pending": len(companies),
            "active": Company.query.filter_by(status="active", deleted_at=None).count(),
            "rejected": Company.query.filter_by(status="rejected", deleted_at=None).count(),
            "users": User.query.count(),
        }
        audit_logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(12).all()
        return render_template("admin.html", companies=companies, users=users, stats=stats, audit_logs=audit_logs, pending_only=True)

    @app.route("/admin/company/<int:company_id>/status", methods=["POST"])
    @app.extensions["limiter"].limit("30 per minute")
    @login_required
    @admin_required
    def admin_company_status(company_id):
        company = Company.query.filter_by(id=company_id, deleted_at=None).first_or_404()
        status = request.form.get("status", "")
        if status not in COMPANY_STATUSES:
            abort(400)
        reason = request.form.get("reason", "").strip()
        if status == "rejected" and not reason:
            flash("Bitte gib beim Ablehnen einen Grund an.", "error")
            return redirect(request.referrer or url_for("admin_dashboard"))

        old_status = company.status
        company.status = status
        description = f"Status geändert: {old_status} -> {status}"
        if reason:
            description = f"{description}. Grund: {reason}"
        add_change(company, current_user, "status_changed", description)
        add_audit(current_user, "company_status_changed", "company", company.id, description)
        db.session.commit()
        if old_status != status:
            notify_company_status_changed(company, old_status, status, reason)
            send_admin_channel_message(f"Status geändert: **{company.name}** {old_status} -> {status}.")
        flash("Status wurde aktualisiert.", "success")
        return redirect(request.referrer or url_for("admin_dashboard"))

    @app.route("/admin/company/<int:company_id>/reject", methods=["POST"])
    @app.extensions["limiter"].limit("30 per minute")
    @login_required
    @admin_required
    def admin_company_reject(company_id):
        company = Company.query.filter_by(id=company_id, deleted_at=None).first_or_404()
        reason = request.form.get("reason", "").strip()
        if not reason:
            flash("Bitte gib beim Ablehnen einen Grund an.", "error")
            return redirect(request.referrer or url_for("admin_dashboard"))

        old_status = company.status
        company.status = "rejected"
        add_change(company, current_user, "rejected", f"Firma wurde abgelehnt. Grund: {reason}")
        add_audit(current_user, "company_rejected", "company", company.id, f"Firma {company.name} wurde abgelehnt. Grund: {reason}")
        db.session.commit()
        if old_status != "rejected":
            notify_company_status_changed(company, old_status, "rejected", reason)
            send_admin_channel_message(f"Firma abgelehnt: **{company.name}**. Grund: {reason}")
        flash("Firma wurde abgelehnt.", "success")
        return redirect(request.referrer or url_for("admin_dashboard"))

    @app.route("/admin/company/<int:company_id>/delete", methods=["POST"])
    @app.extensions["limiter"].limit("10 per minute")
    @login_required
    @admin_required
    def admin_company_delete(company_id):
        company = Company.query.filter_by(id=company_id, deleted_at=None).first_or_404()
        reason = request.form.get("reason", "").strip()
        if not reason:
            flash("Bitte gib beim Löschen einen Grund an.", "error")
            return redirect(request.referrer or url_for("admin_dashboard"))
        company_name = company.name
        owner = company.owner
        company.deleted_at = datetime.now(timezone.utc)
        add_audit(current_user, "company_deleted", "company", company.id, f"Firma {company_name} wurde gelöscht. Grund: {reason}")
        db.session.commit()
        if owner:
            send_discord_dm(owner.discord_id, f"Deine Firma **{company_name}** wurde aus dem Unternehmensregister gelöscht.\nGrund: {reason}")
        send_admin_channel_message(f"Firma gelöscht: **{company_name}**. Grund: {reason}")
        flash(f"Firma {company_name} wurde gelöscht.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/company/<int:company_id>/restore", methods=["POST"])
    @login_required
    @admin_required
    def admin_company_restore(company_id):
        company = db.get_or_404(Company, company_id)
        company.deleted_at = None
        add_audit(current_user, "company_restored", "company", company.id, f"Firma {company.name} wurde wiederhergestellt.")
        db.session.commit()
        flash("Firma wurde wiederhergestellt.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/user/<int:user_id>/role", methods=["POST"])
    @app.extensions["limiter"].limit("20 per minute")
    @login_required
    @admin_required
    def admin_user_role(user_id):
        user = db.get_or_404(User, user_id)
        role = request.form.get("role", "")
        if role not in USER_ROLES:
            abort(400)
        if user.id == current_user.id and role != "admin":
            flash("Du kannst dir selbst nicht die Admin-Rolle entziehen.", "error")
            return redirect(url_for("admin_dashboard"))

        old_role = user.role
        user.role = role
        add_audit(current_user, "user_role_changed", "user", user.id, f"Rolle von {user.username}: {old_role} -> {role}")
        db.session.commit()
        if old_role != role:
            send_discord_dm(user.discord_id, f"Deine Rolle im Unternehmensregister wurde geändert: {old_role} -> {role}.")
        flash(f"Rolle von {user.username} wurde aktualisiert.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/login")
    @app.extensions["limiter"].limit("10 per minute")
    def login():
        client_id = os.getenv("DISCORD_CLIENT_ID")
        client_secret = os.getenv("DISCORD_CLIENT_SECRET")
        redirect_uri = os.getenv("DISCORD_REDIRECT_URI", url_for("callback", _external=True))
        if not client_id or not client_secret:
            flash("Discord OAuth ist noch nicht konfiguriert.", "error")
            return redirect(url_for("index"))

        state = os.urandom(16).hex()
        session["oauth_state"] = state
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "identify",
            "state": state,
            "prompt": "consent",
        }
        return redirect(f"{DISCORD_AUTHORIZE_URL}?{urlencode(params)}")

    @app.route("/callback")
    def callback():
        if request.args.get("error"):
            flash(f"Discord Login fehlgeschlagen: {request.args.get('error_description', request.args['error'])}", "error")
            return redirect(url_for("index"))

        if request.args.get("state") != session.pop("oauth_state", None):
            abort(400)
        code = request.args.get("code")
        if not code:
            flash("Discord Login wurde abgebrochen.", "error")
            return redirect(url_for("index"))

        try:
            token = exchange_discord_code(code)
            discord_user = fetch_discord_user(token["access_token"])
        except KeyError:
            app.logger.exception("Discord OAuth response did not contain an access token")
            flash("Discord Login fehlgeschlagen: Discord hat kein Access Token zurückgegeben.", "error")
            return redirect(url_for("index"))
        except requests.HTTPError as error:
            status_code = error.response.status_code if error.response is not None else "unbekannt"
            error_text = error.response.text if error.response is not None else str(error)
            app.logger.error("Discord OAuth HTTP error %s: %s", status_code, error_text)
            flash(f"Discord Login fehlgeschlagen: Discord API Fehler {status_code}. Details stehen im Server-Log.", "error")
            return redirect(url_for("index"))
        except requests.RequestException:
            app.logger.exception("Discord OAuth request failed")
            flash("Discord Login fehlgeschlagen: Discord API konnte nicht erreicht werden.", "error")
            return redirect(url_for("index"))

        user = upsert_user(discord_user)
        login_user(user)
        flash("Du bist angemeldet.", "success")
        return redirect(url_for("index"))

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        flash("Du bist abgemeldet.", "success")
        return redirect(url_for("index"))

    @app.errorhandler(403)
    def forbidden(error):
        return render_template("error.html", title="Kein Zugriff", message="Du hast keine Berechtigung für diese Aktion."), 403

    @app.errorhandler(404)
    def not_found(error):
        return render_template("error.html", title="Nicht gefunden", message="Diese Seite oder Firma existiert nicht."), 404


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def register_security_headers(app):
    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "form-action 'self'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'",
        )
        return response


def validate_company(company):
    required = [
        (company.name, "Name"),
        (company.short_name, "Kürzel"),
        (company.description, "Beschreibung"),
        (company.industry, "Branche"),
        (company.headquarters, "Sitz"),
        (company.district, "Bezirk"),
    ]
    missing = [label for value, label in required if not value]
    if missing:
        return "Pflichtfelder fehlen: " + ", ".join(missing)
    if company.status not in COMPANY_STATUSES:
        return "Ungültiger Status."
    if len(company.description) < 20:
        return "Die Beschreibung muss mindestens 20 Zeichen lang sein."
    if not re.fullmatch(r"[A-Z0-9-]{2,24}", company.short_name):
        return "Das Kürzel darf nur A-Z, 0-9 und Bindestrich enthalten und muss 2 bis 24 Zeichen lang sein."
    if len(company.short_name) > 24:
        return "Das Kürzel darf maximal 24 Zeichen lang sein."
    duplicate = Company.query.filter(
        Company.id != (company.id or 0),
        Company.deleted_at.is_(None),
        db.or_(Company.name == company.name, Company.short_name == company.short_name),
    ).first()
    if duplicate:
        return "Name oder Kürzel ist bereits vergeben."
    return None


def ensure_schema():
    if db.engine.dialect.name != "sqlite":
        return

    columns = {row[1] for row in db.session.execute(text("PRAGMA table_info(company)")).all()}
    for column_name, ddl in {
        "logo_filename": "ALTER TABLE company ADD COLUMN logo_filename VARCHAR(255)",
        "register_id": "ALTER TABLE company ADD COLUMN register_id VARCHAR(24)",
        "deleted_at": "ALTER TABLE company ADD COLUMN deleted_at DATETIME",
        "parent_company_id": "ALTER TABLE company ADD COLUMN parent_company_id INTEGER",
    }.items():
        if column_name in columns:
            continue
        try:
            db.session.execute(text(ddl))
            db.session.commit()
        except OperationalError as error:
            db.session.rollback()
            if "duplicate column" not in str(error).lower():
                raise
    db.create_all()
    for company in Company.query.filter(Company.register_id.is_(None)).all():
        company.register_id = generate_register_id(company.id)
    db.session.commit()


def generate_register_id(company_id):
    return f"RR-{company_id:04d}"


def add_audit(actor, action, target_type, target_id, description):
    db.session.add(
        AuditLog(
            actor=actor if actor and actor.is_authenticated else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            description=description,
        )
    )


def get_company_suggestions():
    def values(column):
        return [
            row[0]
            for row in db.session.query(column)
            .filter(Company.deleted_at.is_(None), column != "")
            .distinct()
            .order_by(column)
            .all()
        ]

    return {
        "industries": values(Company.industry),
        "districts": values(Company.district),
        "headquarters": values(Company.headquarters),
    }


def save_company_logo(company, file_storage):
    if not file_storage or not file_storage.filename:
        return None

    filename = secure_filename(file_storage.filename)
    extension = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    if extension not in ALLOWED_LOGO_EXTENSIONS:
        return "Logo muss eine Bilddatei sein: png, jpg, jpeg, webp oder gif."

    try:
        image = Image.open(file_storage.stream)
        image.verify()
        file_storage.stream.seek(0)
        image = Image.open(file_storage.stream)
        image.thumbnail((1200, 1200))
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
    except (UnidentifiedImageError, OSError):
        return "Logo konnte nicht als gültiges Bild gelesen werden."

    stored_name = f"company-{company.id}-{uuid4().hex}.png"
    target_path = os.path.join(current_app.config["LOGO_UPLOAD_FOLDER"], stored_name)
    image.save(target_path, format="PNG", optimize=True)
    company.logo_filename = stored_name
    return None


def add_change(company, user, action, description):
    db.session.add(
        CompanyChange(
            company=company,
            user=user if user and user.is_authenticated else None,
            action=action,
            description=description,
        )
    )


def promote_company_actor(user):
    if user.role in ("viewer", "member"):
        user.role = "owner"


def exchange_discord_code(code):
    data = {
        "client_id": os.getenv("DISCORD_CLIENT_ID"),
        "client_secret": os.getenv("DISCORD_CLIENT_SECRET"),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": os.getenv("DISCORD_REDIRECT_URI", "http://localhost:5000/callback"),
    }
    response = requests.post(DISCORD_TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
    response.raise_for_status()
    return response.json()


def fetch_discord_user(access_token):
    response = requests.get(DISCORD_USER_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    response.raise_for_status()
    return response.json()


def send_discord_dm(discord_id, message):
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    if not bot_token:
        current_app.logger.info("Discord bot token missing; skipped DM to %s", discord_id)
        return False

    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }
    try:
        channel_response = requests.post(DISCORD_DM_URL, json={"recipient_id": str(discord_id)}, headers=headers, timeout=10)
        channel_response.raise_for_status()
        channel_id = channel_response.json()["id"]

        message_response = requests.post(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            json={"content": message[:1900]},
            headers=headers,
            timeout=10,
        )
        message_response.raise_for_status()
        current_app.logger.info("Sent Discord DM to %s", discord_id)
        return True
    except (KeyError, requests.RequestException):
        current_app.logger.exception("Could not send Discord DM to %s", discord_id)
        return False


def send_admin_channel_message(message):
    channel_id = os.getenv("DISCORD_ADMIN_CHANNEL_ID", "").strip()
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    if not channel_id or not bot_token:
        return False

    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            json={"content": message[:1900]},
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.RequestException:
        current_app.logger.exception("Could not send Discord admin channel message")
        return False


def notify_company_submitted(company, submitter):
    company_url = url_for("company_detail", company_id=company.id, _external=True)
    admin_discord_ids = get_admin_discord_ids()
    submitter_is_admin = str(submitter.discord_id) in {str(discord_id) for discord_id in admin_discord_ids}

    if submitter_is_admin:
        send_discord_dm(
            submitter.discord_id,
            (
                f"Deine Firma **{company.name}** ({company.short_name}) wurde eingereicht.\n"
                "Du bist als Admin eingetragen und kannst den Antrag direkt im Adminbereich bearbeiten.\n"
                f"{company_url}"
            ),
        )
    else:
        send_discord_dm(
            submitter.discord_id,
            f"Deine Firma **{company.name}** ({company.short_name}) wurde eingereicht und wartet auf Admin-Freigabe.\n{company_url}",
        )

    for discord_id in admin_discord_ids:
        if str(discord_id) == str(submitter.discord_id):
            continue
        send_discord_dm(
            discord_id,
            f"Neue Firma eingereicht: **{company.name}** ({company.short_name}) von {submitter.username}.\n{company_url}",
        )


def notify_company_status_changed(company, old_status, new_status, reason):
    if new_status == "active":
        headline = f"Deine Firma **{company.name}** wurde angenommen."
    elif new_status == "rejected":
        headline = f"Deine Firma **{company.name}** wurde abgelehnt."
    else:
        headline = f"Der Status deiner Firma **{company.name}** wurde geändert: {old_status} -> {new_status}."

    message = headline
    if reason:
        message = f"{message}\nGrund: {reason}"
    message = f"{message}\n{url_for('company_detail', company_id=company.id, _external=True)}"
    send_discord_dm(company.owner.discord_id, message)


def get_admin_discord_ids():
    env_ids = {item.strip() for item in os.getenv("ADMIN_DISCORD_IDS", "").split(",") if item.strip()}
    db_ids = {user.discord_id for user in User.query.filter_by(role="admin").all()}
    return env_ids | db_ids


def upsert_user(discord_user):
    discord_id = discord_user["id"]
    admin_ids = {item.strip() for item in os.getenv("ADMIN_DISCORD_IDS", "").split(",") if item.strip()}
    user = User.query.filter_by(discord_id=discord_id).first()
    if user is None:
        user = User(discord_id=discord_id, username=discord_user.get("username", "Discord User"), avatar=discord_user.get("avatar"))
        db.session.add(user)

    user.username = discord_user.get("username", user.username)
    user.avatar = discord_user.get("avatar")
    if discord_id in admin_ids:
        user.role = "admin"
    db.session.commit()
    return user


def sync_admin_roles():
    admin_ids = {item.strip() for item in os.getenv("ADMIN_DISCORD_IDS", "").split(",") if item.strip()}
    if not admin_ids:
        return

    User.query.filter(User.discord_id.in_(admin_ids)).update({"role": "admin"}, synchronize_session=False)
    db.session.commit()


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
