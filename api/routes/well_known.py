"""
/.well-known/* discovery endpoints.

  GET /.well-known/ucp                      — UCP capability manifest
  GET /.well-known/oauth-authorization-server — OAuth 2.0 metadata (RFC 8414)

These are the entry points that AI agents (Gemini, etc.) use to discover
what this service supports before initiating a checkout flow.
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import get_settings
from services.auth import get_public_jwk

router = APIRouter(tags=["Discovery"])
settings = get_settings()


@router.get(
    "/.well-known/ucp",
    summary="UCP capability manifest",
    description=(
        "Returns the Universal Commerce Protocol capability manifest. "
        "AI agents fetch this endpoint first to discover supported services, "
        "capabilities, payment handlers, and signing keys."
    ),
    response_description="UCP manifest JSON",
)
async def ucp_manifest():
    domain = settings.wc_domain
    manifest = {
        "ucp": {
            "version": settings.ucp_version,
            "services": {
                "dev.ucp.shopping": {
                    "version": settings.ucp_version,
                    "spec": "https://ucp.dev/specs/shopping",
                    "rest": {
                        "schema": "https://ucp.dev/services/shopping/openapi.json",
                        "endpoint": f"{domain}/ucp/",
                    },
                }
            },
            "capabilities": [
                {
                    "name": "dev.ucp.shopping.checkout",
                    "version": settings.ucp_version,
                    "spec": "https://ucp.dev/specs/shopping/checkout",
                    "schema": "https://ucp.dev/schemas/shopping/checkout.json",
                },
                {
                    "name": "dev.ucp.identity.linking",
                    "version": settings.ucp_version,
                    "spec": "https://ucp.dev/specs/identity/linking",
                },
            ],
            "payment_handlers": [
                {
                    "name": "google_pay",
                    "supported_instruments": ["CARD", "WALLET"],
                }
            ],
            "keys": {
                "signing_keys": [get_public_jwk()]
            },
        }
    }
    return JSONResponse(
        content=manifest,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Cache-Control": "public, max-age=3600",
        },
    )


@router.get(
    "/.well-known/oauth-authorization-server",
    summary="OAuth 2.0 authorization server metadata (RFC 8414)",
    description=(
        "Returns OAuth 2.0 authorization server metadata. "
        "Clients use this to discover token and authorization endpoints "
        "before initiating the identity linking flow."
    ),
)
async def oauth_server_metadata():
    domain = settings.wc_domain
    metadata = {
        "issuer": domain,
        "authorization_endpoint": f"{domain}/oauth2/authorize",
        "token_endpoint": f"{domain}/oauth2/token",
        "revocation_endpoint": f"{domain}/oauth2/revoke",
        "jwks_uri": f"{domain}/.well-known/jwks.json",
        "scopes_supported": ["ucp:scopes:checkout_session"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
    }
    return JSONResponse(content=metadata)


@router.get(
    "/.well-known/jwks.json",
    summary="JSON Web Key Set — public keys for JWT verification",
    description="Exposes the RS256 public key used to sign all JWTs issued by this server.",
)
async def jwks():
    return JSONResponse(content={"keys": [get_public_jwk()]})
