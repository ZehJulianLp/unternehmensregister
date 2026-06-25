from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


USER_ROLES = ("member", "owner", "admin")
COMPANY_STATUSES = ("pending", "active", "inactive", "dissolved", "rejected")


def utc_now():
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    discord_id = db.Column(db.String(32), unique=True, nullable=False, index=True)
    username = db.Column(db.String(120), nullable=False)
    avatar = db.Column(db.String(255), nullable=True)
    role = db.Column(db.String(20), nullable=False, default="member")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    companies = db.relationship("Company", back_populates="owner", lazy=True)
    managed_companies = db.relationship("CompanyManager", back_populates="user", cascade="all, delete-orphan")

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_owner(self):
        return self.role in ("owner", "admin")

    def can_edit_company(self, company):
        return self.is_admin or company.owner_id == self.id or any(manager.user_id == self.id for manager in company.managers)


class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    register_id = db.Column(db.String(24), unique=True, nullable=True, index=True)
    name = db.Column(db.String(160), nullable=False)
    short_name = db.Column(db.String(24), nullable=False, unique=True, index=True)
    description = db.Column(db.Text, nullable=False)
    industry = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    headquarters = db.Column(db.String(160), nullable=False)
    district = db.Column(db.String(120), nullable=False)
    logo_filename = db.Column(db.String(255), nullable=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    owner = db.relationship("User", back_populates="companies")
    managers = db.relationship("CompanyManager", back_populates="company", cascade="all, delete-orphan")
    changes = db.relationship(
        "CompanyChange",
        back_populates="company",
        cascade="all, delete-orphan",
        order_by="CompanyChange.created_at.desc()",
    )


class CompanyChange(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    action = db.Column(db.String(60), nullable=False)
    description = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    company = db.relationship("Company", back_populates="changes")
    user = db.relationship("User", lazy=True)


class CompanyManager(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    company = db.relationship("Company", back_populates="managers")
    user = db.relationship("User", back_populates="managed_companies")

    __table_args__ = (db.UniqueConstraint("company_id", "user_id", name="uq_company_manager"),)


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    action = db.Column(db.String(80), nullable=False, index=True)
    target_type = db.Column(db.String(80), nullable=False)
    target_id = db.Column(db.Integer, nullable=True, index=True)
    description = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    actor = db.relationship("User", lazy=True)
