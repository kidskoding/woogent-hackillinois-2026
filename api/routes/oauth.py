"""
OAuth 2.0 Authorization Server endpoints (RFC 6749).

  GET  /oauth2/authorize  — authorization endpoint (renders login form)
  POST /oauth2/token      — token endpoint (code → Bearer JWT)
  POST /oauth2/revoke     — token revocation (RFC 7009)

Supports the Authorization Code grant with HTTP Basic authentication
for the token endpoint (client_secret_basic).

Scope: ucp:scopes:checkout_session
"""
import base64
import secrets

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from typing import Optional

from services.auth import (
    create_authorization_code,
    exchange_code_for_token,
    revoke_token,
    verify_client,
)

router = APIRouter(prefix="/oauth2", tags=["OAuth 2.0"])

# In-memory CSRF state store: state → {client_id, redirect_uri, scope}
_pending_states: dict[str, dict] = {}

_LOGIN_FORM = """
<!DOCTYPE html>
<html>
<head>
  <title>Woogent — Authorize</title>
  <style>
    body {{ font-family: system-ui, sans-serif; display:flex; justify-content:center; align-items:center; min-height:100vh; margin:0; background:#f5f5f5; }}
    .card {{ background:#fff; padding:2rem 2.5rem; border-radius:12px; box-shadow:0 4px 24px rgba(0,0,0,.08); width:360px; }}
    h1 {{ font-size:1.25rem; margin-bottom:.25rem; }}
    p.sub {{ color:#666; font-size:.9rem; margin-bottom:1.5rem; }}
    label {{ font-size:.85rem; font-weight:600; display:block; margin-bottom:.25rem; }}
    input {{ width:100%; padding:.5rem .75rem; border:1px solid #ddd; border-radius:6px; font-size:.95rem; box-sizing:border-box; margin-bottom:1rem; }}
    button {{ width:100%; padding:.65rem; background:#7f54b3; color:#fff; border:none; border-radius:6px; font-size:1rem; cursor:pointer; }}
    button:hover {{ background:#6b46a0; }}
    .scope {{ background:#f0ebf8; border-radius:6px; padding:.5rem .75rem; font-size:.82rem; color:#5a3e8a; margin-bottom:1.25rem; }}
  </style>
</head>
<body>
<div class="card">
  <h1>Authorize Woogent</h1>
  <p class="sub">Client: <strong>{client_id}</strong> is requesting access.</p>
  <div class="scope">Scope: <code>{scope}</code></div>
  <form method="post" action="/oauth2/authorize">
    <input type="hidden" name="state" value="{state}">
    <input type="hidden" name="client_id" value="{client_id}">
    <input type="hidden" name="redirect_uri" value="{redirect_uri}">
    <input type="hidden" name="scope" value="{scope}">
    <label for="username">Username</label>
    <input id="username" name="username" type="text" value="admin" required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" placeholder="WP admin password" required>
    <button type="submit">Authorize</button>
  </form>
</div>
</body>
</html>
"""


@router.get(
    "/authorize",
    response_class=HTMLResponse,
    summary="Authorization endpoint",
    description=(
        "Renders an authorization consent page. "
        "On approval, redirects to redirect_uri with an authorization code. "
        "Required params: client_id, redirect_uri, response_type=code, scope, state."
    ),
)
async def authorize_get(
    client_id: str,
    redirect_uri: str,
    response_type: str,
    scope: str,
    state: str,
):
    if response_type != "code":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "unsupported_response_type"},
        )

    csrf_state = secrets.token_urlsafe(16)
    _pending_states[csrf_state] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "original_state": state,
    }

    return HTMLResponse(
        _LOGIN_FORM.format(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            state=csrf_state,
        )
    )


@router.post(
    "/authorize",
    summary="Authorization form submission",
    include_in_schema=False,
)
async def authorize_post(
    state: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    scope: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
):
    pending = _pending_states.pop(state, None)
    if pending is None:
        raise HTTPException(status_code=400, detail={"error": "invalid_state"})

    # Simplified auth: accept any non-empty credentials for the demo
    # In production, verify against wp_users
    if not username or not password:
        raise HTTPException(status_code=401, detail={"error": "invalid_credentials"})

    code = create_authorization_code(
        client_id=client_id,
        scope=scope,
        redirect_uri=redirect_uri,
        user_id=username,
    )

    redirect_url = f"{redirect_uri}?code={code}&state={pending['original_state']}"
    return RedirectResponse(url=redirect_url, status_code=302)


@router.post(
    "/token",
    summary="Token endpoint",
    description=(
        "Exchange an authorization code for a Bearer access token. "
        "Authenticate with HTTP Basic (client_id:client_secret). "
        "Returns a signed RS256 JWT valid for 3600 seconds."
    ),
)
async def token(
    request: Request,
    grant_type: str = Form(...),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
):
    # Parse HTTP Basic auth header
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_client", "error_description": "HTTP Basic authentication required."},
            headers={"WWW-Authenticate": "Basic"},
        )

    try:
        decoded = base64.b64decode(auth_header[6:]).decode()
        client_id, client_secret = decoded.split(":", 1)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_client"},
        )

    if not verify_client(client_id, client_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_client", "error_description": "Unknown client or invalid credentials."},
        )

    if grant_type != "authorization_code":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "unsupported_grant_type"},
        )

    if not code or not redirect_uri:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_request", "error_description": "code and redirect_uri are required."},
        )

    token_response = exchange_code_for_token(code, client_id, redirect_uri)
    if token_response is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_grant", "error_description": "Authorization code is invalid or expired."},
        )

    return JSONResponse(content=token_response)


@router.post(
    "/revoke",
    summary="Token revocation (RFC 7009)",
    description="Revoke an access token. Always returns 200 per RFC 7009, even if the token is unknown.",
)
async def revoke(token: str = Form(...)):
    revoke_token(token)
    return JSONResponse(content={})
