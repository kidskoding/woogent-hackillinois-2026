"""
WooCommerce database query helpers.

All functions take an AsyncSession and return plain Python dicts/lists —
no ORM models, keeping the layer thin and easy to test.

Key tables used:
  wp_posts          — products (post_type='product')
  wp_postmeta       — product attributes (_price, _sku, _stock_qty, _manage_stock)
  wp_terms          — category/tag names
  wp_term_taxonomy  — taxonomy classification
  wp_term_relationships — product↔category links
  wc_orders         — orders (HPOS schema)
  wc_order_addresses — billing/shipping addresses
  woocommerce_order_items     — line items
  woocommerce_order_itemmeta  — line item metadata
"""
from __future__ import annotations

import re
import time
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db.connection import engine


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

async def get_product(db: AsyncSession, product_id: str) -> Optional[dict]:
    """Fetch a single product by WooCommerce post ID."""
    result = await db.execute(
        text("""
            SELECT
                p.ID            AS id,
                p.post_title    AS title,
                p.post_excerpt  AS short_description,
                p.post_status   AS status,
                MAX(CASE WHEN pm.meta_key = '_price'       THEN pm.meta_value END) AS price,
                MAX(CASE WHEN pm.meta_key = '_regular_price' THEN pm.meta_value END) AS regular_price,
                MAX(CASE WHEN pm.meta_key = '_sale_price'  THEN pm.meta_value END) AS sale_price,
                MAX(CASE WHEN pm.meta_key = '_sku'         THEN pm.meta_value END) AS sku,
                MAX(CASE WHEN pm.meta_key = '_stock_qty'   THEN pm.meta_value END) AS stock_qty,
                MAX(CASE WHEN pm.meta_key = '_manage_stock' THEN pm.meta_value END) AS manage_stock,
                MAX(CASE WHEN pm.meta_key = '_stock_status' THEN pm.meta_value END) AS stock_status
            FROM wp_posts p
            LEFT JOIN wp_postmeta pm ON pm.post_id = p.ID
            WHERE p.ID = :id AND p.post_type = 'product' AND p.post_status = 'publish'
            GROUP BY p.ID
        """),
        {"id": product_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


def _word_variants(term: str) -> list[str]:
    """Return singular/plural variants of a search term for fuzzy keyword matching."""
    out = [term]
    if term.endswith("ies") and len(term) > 3:
        out.append(term[:-3] + "y")
    if term.endswith("s") and len(term) > 2:
        out.append(term[:-1])
    elif len(term) > 2:
        out.append(term + "s")
    seen: set[str] = set()
    return [t for t in out if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]


_AND = " AND "


def _keyword_clauses(query: str, params: dict) -> str:
    """Build AND-joined WHERE clauses for multi-token keyword search."""
    tokens = [t for t in re.split(r"\s+", query.strip().lower()) if t]
    if not tokens:
        return ""
    token_groups = []
    for i, token in enumerate(tokens):
        group_clauses = []
        for j, variant in enumerate(_word_variants(token)):
            key = f"query_{i}_{j}"
            params[key] = f"%{variant}%"
            group_clauses.append(
                f"(LOWER(p.post_title) LIKE :{key} OR LOWER(p.post_excerpt) LIKE :{key})"
            )
        token_groups.append("(" + " OR ".join(group_clauses) + ")")
    return _AND + _AND.join(token_groups)


def _having_clauses(
    min_price: Optional[float],
    max_price: Optional[float],
    in_stock: Optional[bool],
    params: dict,
) -> str:
    """Build HAVING clause for post-aggregation filters (price, stock)."""
    clauses = []
    if min_price is not None:
        clauses.append("CAST(price AS DECIMAL(10,2)) >= :min_price")
        params["min_price"] = min_price
    if max_price is not None:
        clauses.append("CAST(price AS DECIMAL(10,2)) <= :max_price")
        params["max_price"] = max_price
    if in_stock is True:
        clauses.append("stock_status != 'outofstock'")
    elif in_stock is False:
        clauses.append("stock_status = 'outofstock'")
    return (" HAVING " + _AND.join(clauses)) if clauses else ""


async def search_products(
    db: AsyncSession,
    query: str = "",
    category: str = "",
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    in_stock: Optional[bool] = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """Search published products with optional keyword/category/price/stock filters."""
    sql = """
        SELECT
            p.ID            AS id,
            p.post_title    AS title,
            p.post_excerpt  AS short_description,
            MAX(CASE WHEN pm.meta_key = '_price'        THEN pm.meta_value END) AS price,
            MAX(CASE WHEN pm.meta_key = '_sku'          THEN pm.meta_value END) AS sku,
            MAX(CASE WHEN pm.meta_key = '_stock_status' THEN pm.meta_value END) AS stock_status
        FROM wp_posts p
        LEFT JOIN wp_postmeta pm ON pm.post_id = p.ID
        WHERE p.post_type = 'product'
          AND p.post_status = 'publish'
    """
    params: dict = {"limit": limit, "offset": offset}

    if query:
        sql += _keyword_clauses(query, params)

    if category:
        sql += """
            AND p.ID IN (
                SELECT tr.object_id
                FROM wp_term_relationships tr
                JOIN wp_term_taxonomy tt ON tr.term_taxonomy_id = tt.term_taxonomy_id
                JOIN wp_terms t ON tt.term_id = t.term_id
                WHERE tt.taxonomy = 'product_cat' AND t.slug = :category
            )
        """
        params["category"] = category

    sql += " GROUP BY p.ID"
    sql += _having_clauses(min_price, max_price, in_stock, params)
    sql += " ORDER BY p.post_date DESC LIMIT :limit OFFSET :offset"

    result = await db.execute(text(sql), params)
    return [dict(row) for row in result.mappings().all()]


async def check_stock(db: AsyncSession, product_id: str, quantity: int) -> bool:
    """Return True if the product has sufficient stock (or doesn't manage stock)."""
    result = await db.execute(
        text("""
            SELECT
                MAX(CASE WHEN pm.meta_key = '_manage_stock' THEN pm.meta_value END) AS manage_stock,
                MAX(CASE WHEN pm.meta_key = '_stock_qty'    THEN pm.meta_value END) AS stock_qty,
                MAX(CASE WHEN pm.meta_key = '_stock_status' THEN pm.meta_value END) AS stock_status
            FROM wp_postmeta pm
            WHERE pm.post_id = :id
            GROUP BY pm.post_id
        """),
        {"id": product_id},
    )
    row = result.mappings().first()
    if not row:
        return False
    if row["stock_status"] == "outofstock":
        return False
    if row["manage_stock"] == "yes" and row["stock_qty"] is not None:
        return int(row["stock_qty"]) >= quantity
    return True  # stock management off → assume in stock


# ---------------------------------------------------------------------------
# Orders (HPOS — wc_orders tables)
# ---------------------------------------------------------------------------

async def _ensure_hpos_tables(db: AsyncSession) -> None:
    """
    Ensure minimal HPOS tables exist for demo environments where the WooCommerce
    HPOS migration hasn't been run yet.
    """
    # wc_orders — full WooCommerce HPOS schema
    await db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS wp_wc_orders (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                status varchar(200) NOT NULL DEFAULT '',
                currency varchar(20) NOT NULL DEFAULT 'USD',
                type varchar(20) NOT NULL DEFAULT 'shop_order',
                tax_amount decimal(26,8) NOT NULL DEFAULT 0,
                total_amount decimal(26,8) NOT NULL DEFAULT 0,
                billing_email varchar(320) DEFAULT NULL,
                customer_id BIGINT UNSIGNED DEFAULT NULL,
                payment_method varchar(100) DEFAULT NULL,
                payment_method_title text DEFAULT NULL,
                transaction_id varchar(200) DEFAULT NULL,
                ip_address varchar(40) DEFAULT NULL,
                user_agent text DEFAULT NULL,
                customer_note text DEFAULT NULL,
                parent_order_id BIGINT UNSIGNED DEFAULT NULL,
                date_created_gmt datetime DEFAULT NULL,
                date_updated_gmt datetime DEFAULT NULL,
                date_paid_gmt datetime DEFAULT NULL,
                date_completed_gmt datetime DEFAULT NULL,
                created_via varchar(200) DEFAULT NULL,
                order_key varchar(100) DEFAULT '',
                cart_hash varchar(100) DEFAULT '',
                PRIMARY KEY (id),
                KEY order_key (order_key),
                KEY billing_email (billing_email(191)),
                KEY status (status(100))
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """
        )
    )

    # wc_order_addresses
    await db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS wp_wc_order_addresses (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                order_id BIGINT UNSIGNED NOT NULL,
                address_type varchar(20) NOT NULL,
                first_name varchar(200) DEFAULT '',
                last_name varchar(200) DEFAULT '',
                address_1 varchar(400) DEFAULT '',
                city varchar(200) DEFAULT '',
                state varchar(200) DEFAULT '',
                postcode varchar(20) DEFAULT '',
                country varchar(2) DEFAULT 'US',
                email varchar(320) DEFAULT '',
                PRIMARY KEY (id),
                KEY order_id (order_id),
                KEY address_type (address_type)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """
        )
    )

    # wc_orders_meta
    await db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS wp_wc_orders_meta (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                order_id BIGINT UNSIGNED NOT NULL,
                meta_key varchar(255) DEFAULT NULL,
                meta_value longtext,
                PRIMARY KEY (id),
                KEY order_id (order_id),
                KEY meta_key (meta_key(191))
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """
        )
    )

    # woocommerce_order_items
    await db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS wp_woocommerce_order_items (
                order_item_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                order_item_name TEXT NOT NULL,
                order_item_type VARCHAR(200) NOT NULL DEFAULT '',
                order_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
                PRIMARY KEY (order_item_id),
                KEY order_id (order_id),
                KEY order_item_type (order_item_type(191))
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """
        )
    )

    # woocommerce_order_itemmeta
    await db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS wp_woocommerce_order_itemmeta (
                meta_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                order_item_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
                meta_key VARCHAR(255) DEFAULT NULL,
                meta_value LONGTEXT,
                PRIMARY KEY (meta_id),
                KEY order_item_id (order_item_id),
                KEY meta_key (meta_key(191))
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """
        )
    )


async def create_order(
    db: AsyncSession,
    *,
    session_id: str,
    line_items: list[dict],
    billing: dict,
    shipping: dict,
    totals: dict,
    buyer_email: str = "",
    payment_method: str = "stripe",
    transaction_id: str = "",
) -> dict:
    """
    Write a new WooCommerce order using the High-Performance Order Storage schema.
    Returns the new order dict with its generated ID.
    """
    date_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

    # Use a dedicated connection with explicit commit so the order is guaranteed
    # to persist (avoids session/transaction issues with the request-scoped db).
    async with engine.connect() as conn:
        # Ensure required HPOS tables exist BEFORE starting the transaction.
        # MySQL DDL (CREATE TABLE) causes an implicit commit of any active
        # transaction, which would silently break the atomicity of the inserts
        # below and leave wp_posts/wp_wc_orders in an inconsistent state.
        await _ensure_hpos_tables(conn)
        await conn.commit()  # DDL triggers SQLAlchemy autobegin; commit to reset before begin()

        async with conn.begin():

            # 1. Insert into wp_posts with post_type='shop_order' so WooCommerce
            #    admin can find the order. WC uses this ID as the primary key in
            #    wp_wc_orders (HPOS schema) as well.
            post_result = await conn.execute(
            text("""
            INSERT INTO wp_posts
                (post_author, post_date, post_date_gmt, post_content, post_title,
                 post_excerpt, post_status, comment_status, ping_status,
                 post_name, to_ping, pinged, post_content_filtered,
                 post_modified, post_modified_gmt, post_type, comment_count)
            VALUES
                (0, :now, :now, '', '', '', 'wc-processing', 'closed', 'closed',
                 '', '', '', '',
                 :now, :now, 'shop_order', 0)
            """),
                {"now": date_str},
            )
            order_id = post_result.lastrowid

            # 2. Insert into wp_wc_orders using the post ID
            await conn.execute(
            text("""
                INSERT INTO wp_wc_orders
                (id, status, currency, type, tax_amount, total_amount,
                 billing_email, customer_id, payment_method, payment_method_title,
                 transaction_id, date_created_gmt, date_updated_gmt)
            VALUES
                (:id, 'wc-processing', :currency, 'shop_order', :tax, :total,
                 :email, 0, :payment_method, :payment_method_title,
                 :transaction_id, :now, :now)
            """),
            {
                "id": order_id,
            "currency": totals.get("currency_code", "USD"),
            "tax": totals.get("tax_micros", 0) / 1_000_000,
            "total": totals.get("total_micros", 0) / 1_000_000,
            "email": buyer_email,
            "payment_method": payment_method,
            "payment_method_title": payment_method.replace("_", " ").title(),
            "transaction_id": transaction_id or None,
                "now": date_str,
            },
            )

            # 2. Insert billing address
            await conn.execute(
            text("""
                INSERT INTO wp_wc_order_addresses
                    (order_id, address_type, first_name, last_name,
                     address_1, city, state, postcode, country, email)
            VALUES
                (:order_id, 'billing', :first, :last,
                 :address, :city, :state, :postcode, :country, :email)
            """),
            {
                "order_id": order_id,
                "first": billing.get("given_name", ""),
            "last": billing.get("family_name", ""),
            "address": billing.get("street_address", ""),
            "city": billing.get("locality", ""),
            "state": billing.get("administrative_area", ""),
            "postcode": billing.get("postal_code", ""),
            "country": billing.get("country_code", "US"),
                "email": buyer_email,
            },
            )

            # 3. Insert shipping address
            await conn.execute(
            text("""
            INSERT INTO wp_wc_order_addresses
                (order_id, address_type, first_name, last_name,
                 address_1, city, state, postcode, country)
            VALUES
                (:order_id, 'shipping', :first, :last,
                 :address, :city, :state, :postcode, :country)
            """),
            {
                "order_id": order_id,
                "first": shipping.get("given_name", ""),
            "last": shipping.get("family_name", ""),
            "address": shipping.get("street_address", ""),
            "city": shipping.get("locality", ""),
            "state": shipping.get("administrative_area", ""),
            "postcode": shipping.get("postal_code", ""),
                "country": shipping.get("country_code", "US"),
            },
            )

            # 4. Insert line items
            for item in line_items:
                li_result = await conn.execute(
                text("""
                    INSERT INTO wp_woocommerce_order_items
                    (order_item_name, order_item_type, order_id)
                    VALUES (:name, 'line_item', :order_id)
                """),
                {"name": item.get("title", ""), "order_id": order_id},
                )
                li_id = li_result.lastrowid

                # Insert line item metadata
                meta_rows = [
                    ("_product_id", str(item.get("product_id", ""))),
                    ("_qty", str(item.get("quantity", 1))),
                    ("_line_subtotal", str(item.get("subtotal", 0))),
                    ("_line_total", str(item.get("total", 0))),
                ]
                for meta_key, meta_value in meta_rows:
                    await conn.execute(
                        text("""
                            INSERT INTO wp_woocommerce_order_itemmeta
                            (order_item_id, meta_key, meta_value)
                            VALUES (:li_id, :key, :value)
                        """),
                        {"li_id": li_id, "key": meta_key, "value": meta_value},
                    )

            # 5. Store UCP session ID in order meta
            await conn.execute(
                text("""
                    INSERT INTO wp_wc_orders_meta (order_id, meta_key, meta_value)
                    VALUES (:order_id, '_ucp_session_id', :session_id)
                """),
                {"order_id": order_id, "session_id": session_id},
            )
    # conn.begin() auto-commits on exit

    return {
        "order_id": order_id,
        "status": "wc-processing",
    }


# ---------------------------------------------------------------------------
# Shipping (simplified — returns flat-rate options)
# ---------------------------------------------------------------------------

def _friendly_shipping_label(raw: Optional[str]) -> str:
    """Make generic WooCommerce labels (e.g. 'Flat rate') more descriptive."""
    raw = (raw or "").strip()
    if not raw:
        raw = "Shipping"
    # WooCommerce often uses "Flat rate" and "Free shipping" as-is
    if raw.lower() == "flat rate":
        return "Standard Shipping (5–7 business days)"
    if raw.lower() == "free shipping":
        return "Free Shipping"
    if raw.lower() == "local pickup":
        return "Local Pickup"
    return raw


async def get_shipping_options(db: AsyncSession, country_code: str = "US") -> list[dict]:
    """
    Return available shipping methods from WooCommerce shipping zones.
    Falls back to sensible defaults if no zones are configured.
    """
    try:
        result = await db.execute(
            text("""
                SELECT
                    CONCAT(sm.method_id, ':', sm.instance_id) AS id,
                    sm.method_title AS label,
                    sm.cost AS cost
                FROM wp_woocommerce_shipping_zone_methods sm
                JOIN wp_woocommerce_shipping_zones sz ON sm.zone_id = sz.zone_id
                WHERE sm.is_enabled = 1
                ORDER BY sm.method_order
                LIMIT 5
            """)
        )
        rows = result.mappings().all()
        if rows:
            return [
                {
                    "id": row["id"],
                    "label": _friendly_shipping_label(row["label"]),
                    "price_micros": int(float(row["cost"] or 0) * 1_000_000),
                }
                for row in rows
            ]
    except Exception:
        pass

    # Fallback defaults (use friendly labels)
    return [
        {"id": "flat_rate:1", "label": "Standard Shipping (5–7 days)", "price_micros": 5990000},
        {"id": "flat_rate:2", "label": "Express Shipping (2 days)", "price_micros": 12990000},
        {"id": "free_shipping:1", "label": "Free Shipping (orders over $50)", "price_micros": 0},
    ]


# ---------------------------------------------------------------------------
# UCP Checkout Sessions (persisted to survive API restarts)
# ---------------------------------------------------------------------------

SESSION_TTL_SECONDS = 3600


async def _ensure_sessions_table(db: AsyncSession) -> None:
    """Create wp_ucp_sessions table if it doesn't exist."""
    await db.execute(
        text("""
            CREATE TABLE IF NOT EXISTS wp_ucp_sessions (
                id VARCHAR(64) NOT NULL PRIMARY KEY,
                data JSON NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
    )


async def session_create(db: AsyncSession, session_id: str, data: str) -> None:
    """Persist a checkout session to the database."""
    await _ensure_sessions_table(db)
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    await db.execute(
        text("""
            INSERT INTO wp_ucp_sessions (id, data, created_at, updated_at)
            VALUES (:id, :data, :now, :now)
        """),
        {"id": session_id, "data": data, "now": now},
    )


async def session_get(db: AsyncSession, session_id: str) -> Optional[str]:
    """Load session JSON from the database. Returns None if not found or expired."""
    await _ensure_sessions_table(db)
    result = await db.execute(
        text("""
            SELECT data, created_at
            FROM wp_ucp_sessions
            WHERE id = :id
        """),
        {"id": session_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    created = row["created_at"]
    if created:
        # MySQL returns naive datetime; treat as UTC
        created_ts = created.timestamp() if hasattr(created, "timestamp") else 0
        if time.time() - created_ts > SESSION_TTL_SECONDS:
            await db.execute(text("DELETE FROM wp_ucp_sessions WHERE id = :id"), {"id": session_id})
            return None
    return row["data"]


async def session_update(db: AsyncSession, session_id: str, data: str) -> None:
    """Update a persisted checkout session."""
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    await db.execute(
        text("""
            UPDATE wp_ucp_sessions
            SET data = :data, updated_at = :now
            WHERE id = :id
        """),
        {"id": session_id, "data": data, "now": now},
    )
