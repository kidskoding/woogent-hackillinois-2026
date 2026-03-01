"""
Shared FastAPI dependencies.
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from services.auth import verify_token

_bearer = HTTPBearer(auto_error=False)


async def require_auth(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
):
    """Verify Bearer JWT on protected endpoints."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"status": "unauthorized", "messages": [{"type": "error", "code": "MISSING_TOKEN", "content": "Authorization: Bearer token required.", "severity": "fatal"}]},
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = verify_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"status": "unauthorized", "messages": [{"type": "error", "code": "INVALID_TOKEN", "content": "Token is invalid, expired, or revoked.", "severity": "fatal"}]},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload
