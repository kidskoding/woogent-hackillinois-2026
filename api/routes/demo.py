"""
Demo-only shortcut endpoint.

POST /demo/token — issues a Bearer token directly for the Gemini demo UI,
bypassing the browser-redirect OAuth flow.

This endpoint is intentionally only available when DEMO_MODE=true (set in
docker-compose for the demo service). It should never be enabled in production.
"""
import base64
import os

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from services.auth import verify_client, issue_token

router = APIRouter(prefix="/demo", tags=["Demo"])

DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"


@router.post(
    "/token",
    summary="Demo token shortcut (DEMO_MODE only)",
    description=(
        "Issues a Bearer token directly using HTTP Basic client credentials. "
        "**Only available when DEMO_MODE=true.** "
        "Use this to skip the browser-redirect OAuth flow during live demos."
    ),
)
async def demo_token(request: Request):
    if not DEMO_MODE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        raise HTTPException(status_code=401, detail={"error": "invalid_client"})

    try:
        decoded = base64.b64decode(auth_header[6:]).decode()
        client_id, client_secret = decoded.split(":", 1)
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid_client"})

    if not verify_client(client_id, client_secret):
        raise HTTPException(status_code=401, detail={"error": "invalid_client"})

    token_response = issue_token(user_id="demo-user", scope="ucp:scopes:checkout_session")
    return JSONResponse(content=token_response)
