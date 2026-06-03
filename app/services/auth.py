import hashlib
import secrets

from cryptography.fernet import Fernet


def generate_key() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    return raw, hashed


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def verify_key(raw: str, hashed: str) -> bool:
    return secrets.compare_digest(hash_key(raw), hashed)


def encrypt_backend_key(raw: str, secret: str) -> str:
    f = Fernet(secret.encode())
    return f.encrypt(raw.encode()).decode()


def decrypt_backend_key(encrypted: str, secret: str) -> str:
    f = Fernet(secret.encode())
    return f.decrypt(encrypted.encode()).decode()
