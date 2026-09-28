import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


def cipher():
    # A purpose-specific key derived from the existing persistent app secret.
    secret = current_app.config["SECRET_KEY"]
    key = hashlib.sha256(("register-oidc-tokens:" + secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(value):
    return cipher().encrypt(value.encode()).decode()


def decrypt_secret(value):
    try:
        return cipher().decrypt(value.encode()).decode()
    except (ValueError, InvalidToken):
        return None
