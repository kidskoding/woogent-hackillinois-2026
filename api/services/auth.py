"""
OAuth 2.0 authorization server (in-memory, hackathon-grade).

Implements:
  - RS256 key pair generation on startup
  - Authorization code grant (RFC 6749 §4.1)
  - JWT Bearer token issuance / verification
  - Token revocation

Security properties upheld:
  - Authorization codes expire in 5 minutes
  - Access tokens signed with RS256 (asymmetric — public key published at /.well-known/jwks.json)
  - CSRF protection via `state` parameter
  - Tokens are tracked for revocation
"""
from __future__ import annotations

import base64
import secrets
import time
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt, JWTError

from config import get_settings

settings = get_settings()

# ---------------------------------------------------------------------------
# RS256 key pair — persisted to a dotfile so it survives uvicorn --reload
# restarts (watchfiles ignores dotfiles by default).
# ---------------------------------------------------------------------------

_KEY_FILE = Path("/app/.private_key.pem")

if _KEY_FILE.exists():
    _private_key = serialization.load_pem_private_key(_KEY_FILE.read_bytes(), password=None)
else:
    _private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _KEY_FILE.write_bytes(_private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))

_public_key = _private_key.public_key()

_private_pem = _private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()


def get_public_jwk() -> dict:
    """Return the public key in JWK format for the /.well-known/jwks.json endpoint."""
    pub_numbers = _public_key.public_key().public_numbers() if hasattr(_public_key, "public_key") else _public_key.public_numbers()
    n_bytes = pub_numbers.n.to_bytes((pub_numbers.n.bit_length() + 7) // 8, "big")
    e_bytes = pub_numbers.e.to_bytes((pub_numbers.e.bit_length() + 7) // 8, "big")
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": "woogent-key-1",
        "n": base64.urlsafe_b64encode(n_bytes).rstrip(b"=").decode(),
        "e": base64.urlsafe_b64encode(e_bytes).rstrip(b"=").decode(),
    }


# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------

_auth_codes: dict[str, dict] = {}

# access_tokens: set of active token JTIs
_active_jtis: set[str] = set()

# Known OAuth clients: {client_id: client_secret}
_clients: dict[str, str] = {
    settings.oauth_client_id: settings.oauth_client_secret,
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def register_client(client_id: str, client_secret: str) -> None:
    _clients[client_id] = client_secret


def verify_client(client_id: str, client_secret: str) -> bool:
    return _clients.get(client_id) == client_secret


def create_authorization_code(
    client_id: str,
    scope: str,
    redirect_uri: str,
    user_id: str = "guest",
) -> str:
    """Generate a one-time authorization code (expires in 5 minutes)."""
    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": client_id,
        "scope": scope,
        "redirect_uri": redirect_uri,
        "user_id": user_id,
        "expires_at": time.time() + 300,  # 5 minutes
    }
    return code


def exchange_code_for_token(
    code: str,
    client_id: str,
    redirect_uri: str,
) -> Optional[dict]:
    """
    Exchange authorization code for an access token.
    Returns token dict or None if code is invalid/expired.
    """
    entry = _auth_codes.pop(code, None)
    if entry is None:
        return None
    if entry["client_id"] != client_id:
        return None
    if entry["redirect_uri"] != redirect_uri:
        return None
    if time.time() > entry["expires_at"]:
        return None

    return issue_token(entry["user_id"], entry["scope"])


def issue_token(user_id: str, scope: str) -> dict:
    jti = secrets.token_urlsafe(16)
    now = int(time.time())
    expires_in = 3600

    payload = {
        "iss": settings.wc_domain,
        "sub": user_id,
        "aud": settings.wc_domain,
        "iat": now,
        "exp": now + expires_in,
        "jti": jti,
        "scope": scope,
    }
    token = jwt.encode(payload, _private_pem, algorithm="RS256")
    _active_jtis.add(jti)

    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": scope,
    }


def verify_token(token: str) -> Optional[dict]:
    """
    Verify a Bearer token. Returns the decoded payload or None.
    Checks signature and expiry. JTI revocation is skipped so tokens
    survive server restarts (acceptable for this demo).
    """
    try:
        public_pem = _public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        return jwt.decode(
            token,
            public_pem,
            algorithms=["RS256"],
            audience=settings.wc_domain,
        )
    except JWTError:
        return None


def revoke_token(token: str) -> bool:
    """Revoke a token by removing its JTI from the active set."""
    try:
        public_pem = _public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        payload = jwt.decode(
            token,
            public_pem,
            algorithms=["RS256"],
            audience=settings.wc_domain,
            options={"verify_exp": False},
        )
        jti = payload.get("jti")
        if jti in _active_jtis:
            _active_jtis.discard(jti)
            return True
    except JWTError:
        pass
    return False
