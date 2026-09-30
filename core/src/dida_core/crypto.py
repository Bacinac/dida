"""Shared secret derivation — one place that turns DIDA_SECRET_KEY + a purpose
domain into a Fernet, into each broker client's key and each database role's password.

Only the api (and the one-shot keys service) holds the root key: it encrypts and
decrypts at rest for everyone, adapters included, and a drift in the derivation
would silently break decryption of what is already stored. cryptography is
imported lazily so core stays light where nothing is encrypted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cryptography.fernet import Fernet


def derive_fernet(secret: str, *, domain: str) -> Fernet:
    """Fernet keyed by ``sha256("<domain>:<secret>")``. ``domain`` is a stable
    version tag (a purpose separator, e.g. ``dida-config-v1`` / ``dida-token-v1``),
    NOT a salt — it MUST stay byte-identical or existing ciphertext won't decrypt.

    An empty ``secret`` is rejected loudly: it would key every install identically
    off the (public) domain string — i.e. plaintext-at-rest — and on the default
    install (DIDA_SECRET_KEY unset) it silently diverged from the API's own key,
    breaking every cross-service secret. Fail here so that never happens quietly."""
    from cryptography.fernet import Fernet

    if not secret:
        raise ValueError(
            "DIDA_SECRET_KEY is empty — cannot derive an encryption key. "
            "Set DIDA_SECRET_KEY on the api (and the keys service)."
        )
    key = base64.urlsafe_b64encode(hashlib.sha256(f"{domain}:{secret}".encode()).digest())
    return Fernet(key)


def adapter_key(secret: str, adapter: str) -> str:
    """The key one adapter seals its broker requests with — HMAC of the root key,
    so the api can derive every adapter's key and no adapter can derive another's.
    The root key itself never enters an adapter: it signs every login token, and an
    adapter holding it could mint an admin session."""
    if not secret:
        raise ValueError("DIDA_SECRET_KEY is empty — cannot derive an adapter key.")
    mac = hmac.new(secret.encode(), f"dida-adapter-v1:{adapter}".encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode()


def db_password(secret: str, role: str) -> str:
    """A database role's password, derived like a broker key: the keys service sets
    it on the role and writes it where only that role's services mount it, so the
    superuser's is the only database password in `.env`."""
    if not secret:
        raise ValueError("DIDA_SECRET_KEY is empty — cannot derive a database password.")
    mac = hmac.new(secret.encode(), f"dida-db-v1:{role}".encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")


def bus_password(secret: str, identity: str) -> str:
    """One bus identity's NATS password, derived like a broker key: the keys service
    writes it into the server's user list and into the directory only that identity
    mounts, so no bus password lives in `.env`."""
    if not secret:
        raise ValueError("DIDA_SECRET_KEY is empty — cannot derive a bus password.")
    mac = hmac.new(secret.encode(), f"dida-bus-v1:{identity}".encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")
