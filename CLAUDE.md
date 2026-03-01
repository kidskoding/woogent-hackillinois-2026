# WooGent — Claude Code Instructions

## Project
HackIllinois hackathon — Stripe "Best Web API" track.
WooCommerce UCP (Universal Commerce Protocol) REST API + Gemini AI shopping demo.

## Package Manager
**Always use `uv`.** Never use `pip` directly for local development.
- Install deps: `uv sync`
- Add a package: `uv add <package>`
- Run a script: `uv run python <script>`
- The venv is at `.venv/` — activate with `source .venv/bin/activate`

## Running the Project
```bash
# Start all services
docker compose up -d

# Watch logs
docker compose logs -f api

# Restart just the API after code changes
docker compose restart api

# Run API locally (outside Docker, needs MySQL running)
source .venv/bin/activate
cd api && uvicorn main:app --reload
```

## Key URLs
| Service | URL |
|---------|-----|
| FastAPI docs | http://localhost:8000/docs |
| UCP manifest | http://localhost:8000/.well-known/ucp |
| Gemini demo | http://localhost:7860 |
| WordPress admin | http://localhost:8080/wp-admin (admin / adminpassword) |
| MySQL | localhost:3306 (wordpress / wordpress) |

## Project Structure
```
woogent/
├── pyproject.toml          # uv project — all dependencies live here
├── docker-compose.yml      # 4 services: mysql, wordpress, api, demo
├── .env                    # secrets — never commit
├── api/                    # FastAPI UCP server
│   ├── main.py             # app entry point, router registration
│   ├── config.py           # pydantic-settings from .env
│   ├── models/ucp.py       # all UCP Pydantic models
│   ├── db/
│   │   ├── connection.py   # async SQLAlchemy engine
│   │   └── woo_queries.py  # WooCommerce MySQL queries
│   ├── services/
│   │   ├── auth.py         # RS256 key gen, OAuth 2.0, JWT
│   │   ├── ucp_adapter.py  # UCP ↔ WooCommerce business logic
│   │   └── session_store.py # in-memory checkout session store
│   └── routes/
│       ├── well_known.py   # /.well-known/ucp and oauth-authorization-server
│       ├── checkout.py     # /ucp/checkout-sessions CRUD
│       ├── oauth.py        # /oauth2/authorize, /token, /revoke
│       ├── products.py     # /ucp/products search
│       └── demo.py         # /demo/token shortcut (DEMO_MODE only)
├── demo/
│   └── app.py              # Gradio + Gemini function-calling chat UI
└── wordpress/
    └── setup.sh            # WP-CLI: installs WooCommerce + sample data
```

## Architecture Rules
- **Routes stay thin** — all business logic in `services/`, all DB access in `db/`
- **All money is `amount_micros`** — integer, currency × 1,000,000. Never use floats for prices.
- **UCP error format** — always return `UCPError` model (see `models/ucp.py`) on errors, not plain strings
- **Async everywhere** — all DB calls use `async/await` with the SQLAlchemy async session
- **No ORM models** — `woo_queries.py` returns plain dicts from raw SQL; WooCommerce schema is complex enough that ORM adds no value

## WooCommerce DB Tables
| Table | Purpose |
|-------|---------|
| `wp_posts` | Products (`post_type='product'`) |
| `wp_postmeta` | Product attributes (`_price`, `_sku`, `_stock_qty`) |
| `wc_orders` | Orders (HPOS schema — NOT `wp_posts`) |
| `wc_order_addresses` | Billing/shipping per order |
| `woocommerce_order_items` | Line items |
| `woocommerce_order_itemmeta` | Line item metadata |
| `woocommerce_sessions` | Cart/session data |

## UCP Spec
- Version: `2026-01-11`
- Spec: https://ucp.dev
- Checkout guide: https://developers.google.com/merchant/ucp/guides/checkout/native
- Session state machine: `incomplete` → `pending_payment` → `completed` / `cancelled`

## Auth
- OAuth 2.0 Authorization Code grant (RFC 6749)
- JWTs signed with RS256 (key generated at startup, public key at `/.well-known/jwks.json`)
- All checkout endpoints require `Authorization: Bearer <token>`
- State-mutating endpoints also require `Idempotency-Key` and `Request-Id` headers
- `DEMO_MODE=true` enables `/demo/token` which skips the browser redirect for the Gradio demo

## Git / Commits
When creating or amending commits, append the appropriate co-author line depending on which assistant created the commit:
- Cursor: `Co-authored-by: Cursor <cursoragent@cursor.com>`
- Claude: `Co-authored-by: Claude <noreply@anthropic.com>`

## .env Required Keys
```
GEMINI_API_KEY=           # required for demo/app.py
OAUTH_CLIENT_ID=gemini-demo-client
OAUTH_CLIENT_SECRET=gemini-demo-secret
SECRET_KEY=               # JWT signing seed
```
