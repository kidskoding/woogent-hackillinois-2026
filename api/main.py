"""
Woogent — WooCommerce Universal Commerce Protocol (UCP) API

Implements the Google UCP spec (https://ucp.dev) on top of a WooCommerce
MySQL database, enabling AI agents (Gemini, etc.) to discover, browse,
and checkout from a WooCommerce store.

Spec version: 2026-01-11
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy import text
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import get_settings
from routes.well_known import router as well_known_router
from routes.checkout import router as checkout_router
from routes.oauth import router as oauth_router
from routes.orders import router as orders_router
from routes.products import router as products_router
from routes.demo import router as demo_router

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up: import auth module to generate RSA key pair on startup
    import services.auth  # noqa: F401
    yield


app = FastAPI(
    title="Woogent UCP API",
    description="""
## WooCommerce Universal Commerce Protocol API

This API implements Google's [Universal Commerce Protocol (UCP)](https://ucp.dev)
on top of WooCommerce, enabling AI agents like Gemini to discover, browse, and
checkout from a WooCommerce store autonomously.

### First request (quickstart)

1. **No auth needed:** `GET /.well-known/ucp` — returns the capability manifest.
2. **Browse products:** `GET /ucp/products?q=hoodie&limit=5` — no auth required.
3. **Checkout (requires token):** Get a token via OAuth (`/oauth2/authorize` → `/oauth2/token`) or, in demo mode, `POST /demo/token` with `client_id` and `client_secret`. Then call `POST /ucp/checkout-sessions` with `Authorization: Bearer <token>` and required headers (see Checkout section).

### How it works

1. **Discovery**: AI agents start at `/.well-known/ucp` to learn what this service supports.
2. **Browse**: Use `GET /ucp/products` to search the product catalog.
3. **Checkout**: Create a session → add address → select shipping → complete.
4. **Identity**: OAuth 2.0 (Authorization Code grant) for secure account linking.

### Authentication

All checkout (and order) endpoints require a Bearer token obtained via OAuth 2.0:

```
GET /oauth2/authorize?client_id=...&redirect_uri=...&response_type=code&scope=ucp:scopes:checkout_session&state=...
POST /oauth2/token  (HTTP Basic + authorization code)
```

State-mutating checkout calls also require: **Idempotency-Key** (UUID), **Request-Id**, and **UCP-Agent** headers.

### Prices

All monetary values use `amount_micros` (integer, currency × 1,000,000):
- `$19.99` → `amount_micros: 19990000`

### Error codes (UCP format)

Errors return `{"status": "...", "messages": [{"type": "error", "code": "...", "content": "...", "severity": "fatal"|"recoverable"}]}`.

| Code | HTTP | Meaning |
|------|------|---------|
| `MISSING_IDEMPOTENCY_KEY` | 400 | Idempotency-Key header required on state-mutating checkout/order calls. |
| `MISSING_TOKEN` | 401 | Authorization: Bearer header required. |
| `INVALID_TOKEN` | 401 | Token invalid, expired, or revoked. |
| `SESSION_NOT_FOUND` | 404 | Checkout session ID unknown or expired. |
| `PRODUCT_NOT_FOUND` | 404 | Product ID not found. |
| `ORDER_NOT_FOUND` | 404 | Order ID not found. |
""",
    version="1.0.0",
    contact={
        "name": "Woogent",
        "url": "https://github.com/woogent",
    },
    license_info={
        "name": "MIT",
    },
    openapi_tags=[
        {"name": "Discovery", "description": "UCP manifest and OAuth server metadata."},
        {"name": "Products", "description": "Search and retrieve product catalog."},
        {"name": "OAuth 2.0", "description": "Authorization and token endpoints."},
        {"name": "Checkout", "description": "Create and manage checkout sessions."},
        {"name": "Orders", "description": "List, get, and cancel orders."},
        {"name": "Demo", "description": "Demo token shortcut (DEMO_MODE only)."},
        {"name": "System", "description": "Health and utility."},
    ],
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# ---------------------------------------------------------------------------
# CORS — allow AI agents from any origin
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Request-Id"],
)


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(well_known_router)
app.include_router(oauth_router)
app.include_router(demo_router)
app.include_router(products_router)
app.include_router(checkout_router)
app.include_router(orders_router)


# ---------------------------------------------------------------------------
# Root + Health
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "Woogent UCP API",
        "ucp_version": settings.ucp_version,
        "discovery": f"{settings.wc_domain}/.well-known/ucp",
        "docs": f"{settings.wc_domain}/docs",
        "spec": "https://ucp.dev",
    }


@app.get(
    "/health",
    tags=["System"],
    summary="Health check",
    description="Returns service status and DB connectivity. Use this to verify the API is running.",
)
async def health():
    from db.connection import engine
    db_ok = False
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    return {
        "status": "ok" if db_ok else "degraded",
        "database": "connected" if db_ok else "unavailable",
        "ucp_version": settings.ucp_version,
    }


# ---------------------------------------------------------------------------
# Global exception handler — always return UCP error format
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "status": "internal_error",
            "messages": [
                {
                    "type": "error",
                    "code": "INTERNAL_SERVER_ERROR",
                    "content": "An unexpected error occurred.",
                    "severity": "fatal",
                }
            ],
        },
    )
