"""Ed25519-signed login tokens ("v2") for the *.example.org and *.example.net fleets.

Only the issuer holds SESSION_SIGNING_KEY. Every other app gets SESSION_PUBLIC_KEYS, which can check a
login but can't create one.

Token: v2.<payload>.<sig>, payload = base64url(JSON claims), sig = Ed25519 over b"v2." + payload.
"""

from __future__ import annotations

import base64
import functools
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

MAX_AGE = 30 * 24 * 3600
SKEW = 60
MAX_PUBLIC_KEYS = 2
RESERVED_CLAIMS = ("iss", "kid", "iat", "exp")

_KID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class SigningKey(NamedTuple):
    kid: str
    key: Ed25519PrivateKey


class PublicKey(NamedTuple):
    kid: str
    key: Ed25519PublicKey


@dataclass(frozen=True)
class Settings:
    issuer: str | None
    public_keys: tuple[PublicKey, ...]


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    """Decode unpadded base64url. The round trip refuses padding, the +/ alphabet, junk and non-canonical forms."""
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if _b64e(raw) != text:
        raise ValueError("non-canonical base64url")
    return raw


def _split_entry(entry: str, size: int, what: str) -> tuple[str, bytes]:
    """Split '<kid>:<b64url>' and check the decoded length. Errors never quote the value."""
    kid, sep, b64 = entry.partition(":")
    if not sep or not _KID.fullmatch(kid):
        raise ValueError(f"{what}: expected <kid>:<base64url>")
    try:
        raw = _b64d(b64)
    except ValueError:
        raise ValueError(f"{what}: key is not unpadded base64url") from None
    if len(raw) != size:
        raise ValueError(f"{what}: key must be {size} bytes, got {len(raw)}")
    return kid, raw


def parse_signing_key(value: str) -> SigningKey:
    """Parse SESSION_SIGNING_KEY ('<kid>:<base64url 32-byte seed>')."""
    kid, seed = _split_entry(value.strip(), 32, "SESSION_SIGNING_KEY")
    return SigningKey(kid, Ed25519PrivateKey.from_private_bytes(seed))


def parse_public_keys(value: str) -> tuple[PublicKey, ...]:
    """Parse SESSION_PUBLIC_KEYS ('<kid>:<b64url>[,<kid>:<b64url>]', at most two)."""
    entries = value.strip().split(",")
    if len(entries) > MAX_PUBLIC_KEYS:
        raise ValueError(f"SESSION_PUBLIC_KEYS: at most {MAX_PUBLIC_KEYS} keys, got {len(entries)}")
    keys = []
    for entry in entries:
        kid, raw = _split_entry(entry, 32, "SESSION_PUBLIC_KEYS")
        keys.append(PublicKey(kid, Ed25519PublicKey.from_public_bytes(raw)))
    if len({k.kid for k in keys}) != len(keys):
        raise ValueError("SESSION_PUBLIC_KEYS: duplicate kid")
    return tuple(keys)


def generate_keypair(kid: str) -> tuple[str, str]:
    """Return a fresh (SESSION_SIGNING_KEY value, SESSION_PUBLIC_KEYS entry) pair."""
    if not _KID.fullmatch(kid):
        raise ValueError("kid must match [A-Za-z0-9._-]{1,64}")
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return f"{kid}:{_b64e(seed)}", f"{kid}:{_b64e(pub)}"


def _email(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.lower()


def sign_v2(
    claims: Mapping[str, Any], signing_key: SigningKey, *, issuer: str, now: int, ttl: int = MAX_AGE
) -> str:
    """Mint a v2 token (issuer only). `claims` carries email (+ user_id); iss/kid/iat/exp are set here."""
    if not 0 < ttl <= MAX_AGE:
        raise ValueError(f"ttl must be 1..{MAX_AGE}")
    if any(k in claims for k in RESERVED_CLAIMS):
        raise ValueError(f"claims must not set {', '.join(RESERVED_CLAIMS)}")
    if _email(claims.get("email")) is None:
        raise ValueError("claims need a non-empty email")
    body = {"iss": issuer, "kid": signing_key.kid, **claims, "iat": now, "exp": now + ttl}
    payload = _b64e(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode())
    sig = signing_key.key.sign(f"v2.{payload}".encode("ascii"))
    return f"v2.{payload}.{_b64e(sig)}"


def _verifies(key: Ed25519PublicKey, sig: bytes, data: bytes) -> bool:
    try:
        key.verify(sig, data)
    except InvalidSignature:
        return False
    return True


def verify_v2(
    token: str, *, issuer: str | None, public_keys: Sequence[PublicKey], now: float
) -> dict[str, Any] | None:
    """Return the token's claims (email lowercased) if every v2 rule passes, else None."""
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v2" or not issuer:
        return None
    try:
        payload, sig = _b64d(parts[1]), _b64d(parts[2])
        signed = f"v2.{parts[1]}".encode("ascii")
        # kid is informational: try each published key, so a mislabelled kid can't select a key.
        if not any(_verifies(k.key, sig, signed) for k in public_keys):
            return None
        claims = json.loads(payload.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(claims, dict) or claims.get("iss") != issuer:
        return None
    iat, exp = claims.get("iat"), claims.get("exp")
    if type(iat) is not int or type(exp) is not int:
        return None
    if iat > now + SKEW or now >= exp or exp - iat > MAX_AGE + SKEW:
        return None
    email = _email(claims.get("email"))
    return {**claims, "email": email} if email else None


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    """Read SESSION_PUBLIC_KEYS and SESSION_ISSUER. Raise on bad or missing prod config."""
    # Dokku sets DOKKU_APP_TYPE in every app's env; DOKKU_APP_NAME is not set in containers.
    prod = bool(env.get("DOKKU_APP_TYPE") or env.get("DOKKU_APP_NAME"))
    raw_keys, issuer = env.get("SESSION_PUBLIC_KEYS", ""), env.get("SESSION_ISSUER", "")
    if prod and not (raw_keys and issuer):
        raise RuntimeError("SESSION_PUBLIC_KEYS and SESSION_ISSUER are required in production")
    keys = parse_public_keys(raw_keys) if raw_keys else ()
    return Settings(issuer=issuer or None, public_keys=keys)


@functools.cache
def session_settings() -> Settings:
    """load_settings(os.environ), read once. Call it at startup to fail fast on bad config."""
    return load_settings()


def verify_session(
    token: str | None, *, settings: Settings | None = None, now: float | None = None
) -> dict[str, Any] | None:
    """Verify a `session` cookie value. Returns the claims with `email` lowercased, or None.

    App checks (owner, allowlist) stay in the app.
    """
    if not token:
        return None
    s = settings or session_settings()
    return verify_v2(
        token, issuer=s.issuer, public_keys=s.public_keys, now=time.time() if now is None else now
    )


@functools.cache
def _issuer_key() -> SigningKey:
    return parse_signing_key(os.environ["SESSION_SIGNING_KEY"])


def issue_session(claims: Mapping[str, Any], *, ttl: int = MAX_AGE, now: int | None = None) -> str:
    """Mint a session token from SESSION_SIGNING_KEY (issuer app only); refuse if its own verifier would reject it."""
    s = session_settings()
    if not s.issuer:
        raise RuntimeError("SESSION_ISSUER is not set")
    at = int(time.time()) if now is None else now
    token = sign_v2(claims, _issuer_key(), issuer=s.issuer, now=at, ttl=ttl)
    if verify_v2(token, issuer=s.issuer, public_keys=s.public_keys, now=at) is None:
        raise RuntimeError("SESSION_SIGNING_KEY does not match SESSION_PUBLIC_KEYS")
    return token
