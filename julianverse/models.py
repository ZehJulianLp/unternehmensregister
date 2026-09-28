import time

from models import db


def now():
    return int(time.time())


class JulianverseIdentity(db.Model):
    __table_args__ = (db.UniqueConstraint("issuer", "subject"),)
    user_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    issuer = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    linked_at = db.Column(db.Integer, default=now, nullable=False)
    user = db.relationship("User", back_populates="julianverse_identity")
    sessions = db.relationship(
        "JulianverseSession", back_populates="identity", cascade="all, delete-orphan"
    )


class JulianverseSession(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(
        db.ForeignKey("julianverse_identity.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    identity = db.relationship(JulianverseIdentity, back_populates="sessions")
    secret = db.Column(db.Text, nullable=False)
    expires_at = db.Column(db.Integer, nullable=False)
    checked_at = db.Column(db.Integer, nullable=False, default=now)
    confirmed_at = db.Column(db.Integer, nullable=False, default=0)
    lease_until = db.Column(db.Integer, nullable=False, default=0)
    lease_key = db.Column(db.String(64), nullable=True)


class JulianverseAttempt(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    browser_hash = db.Column(db.String(64), nullable=False)
    purpose = db.Column(db.String(32), nullable=False)
    user_id = db.Column(db.Integer, nullable=True)
    identity_stamp = db.Column(db.String(64), nullable=True)
    next_path = db.Column(db.Text, nullable=False, default="/")
    expires_at = db.Column(db.Integer, nullable=False)
    used = db.Column(db.Boolean, nullable=False, default=False)
