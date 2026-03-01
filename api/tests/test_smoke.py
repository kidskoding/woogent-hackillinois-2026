from fastapi.testclient import TestClient

from main import app
from db.connection import get_db
from db import woo_queries as woo_q


# ---------------------------------------------------------------------------
# Dependency overrides (avoid real DB)
# ---------------------------------------------------------------------------

async def _override_get_db():
    # Yield a dummy DB session; tests that hit the DB layer
    # monkeypatch the query functions to ignore this argument.
    yield None


app.dependency_overrides[get_db] = _override_get_db

client = TestClient(app)


def test_root_and_health_up():
    # Root should return basic service metadata
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body.get("service") == "Woogent UCP API"
    assert "ucp_version" in body

    # Health endpoint should respond (even if DB is degraded in local env)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "status" in body
    assert "database" in body


def test_discovery_endpoints():
    # UCP manifest
    r = client.get("/.well-known/ucp")
    assert r.status_code == 200
    manifest = r.json()
    assert "ucp" in manifest
    assert "services" in manifest["ucp"]
    assert "capabilities" in manifest["ucp"]

    # OAuth metadata
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    meta = r.json()
    assert meta.get("authorization_endpoint")
    assert meta.get("token_endpoint")
    assert meta.get("jwks_uri")

    # JWKS
    r = client.get("/.well-known/jwks.json")
    assert r.status_code == 200
    jwks = r.json()
    assert "keys" in jwks
    assert isinstance(jwks["keys"], list)
    assert jwks["keys"], "expected at least one JWK"


def test_products_list_uses_ucp_format(monkeypatch):
    # Monkeypatch search_products to avoid DB access and return a fake product
    async def fake_search_products(db, query, category, max_price, limit, offset):
        return [
            {
                "id": 1,
                "title": "Demo Hoodie",
                "short_description": "A comfy demo hoodie",
                "sku": "DEMO-HOODIE",
                "price": "19.99",
                "stock_status": "instock",
            }
        ]

    monkeypatch.setattr(woo_q, "search_products", fake_search_products)

    r = client.get("/ucp/products?limit=1")
    assert r.status_code == 200
    body = r.json()
    assert body.get("count") == 1
    assert body.get("products")

    product = body["products"][0]
    assert product["id"] == "1"
    assert product["title"] == "Demo Hoodie"
    assert product["price"]["amount_micros"] == 19_990_000
    assert product["price"]["currency_code"] == "USD"

