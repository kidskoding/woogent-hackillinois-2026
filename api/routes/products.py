"""
Product discovery endpoint.

  GET /ucp/products — search/list WooCommerce products

This endpoint enables AI agents to browse the catalog before building
a cart. Gemini can call this to answer questions like
"show me blue hoodies under $50".
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from db.connection import get_db
from db.woo_queries import get_product, search_products
from models.ucp import UCPError, UCPMessage
from config import get_settings

settings = get_settings()
router = APIRouter(prefix="/ucp", tags=["Products"])


def _format_product(p: dict) -> dict:
    """Convert a WooCommerce DB row to a UCP-friendly product dict."""
    try:
        price_micros = int(round(float(p.get("price") or 0) * 1_000_000))
    except (TypeError, ValueError):
        price_micros = 0

    return {
        "id": str(p["id"]),
        "title": p.get("title", ""),
        "short_description": p.get("short_description", ""),
        "sku": p.get("sku", ""),
        "price": {
            "amount_micros": price_micros,
            "currency_code": "USD",
        },
        "in_stock": p.get("stock_status", "instock") != "outofstock",
        "url": f"{settings.wp_domain}/?p={p['id']}",
    }


@router.get(
    "/products",
    summary="Search and list products",
    description=(
        "Returns WooCommerce products matching the given filters. "
        "AI agents use this endpoint to browse the catalog before initiating checkout. "
        "**Pagination:** Use `limit` and `offset`; when `has_more` is true, request the next page with `offset + limit`."
        "\n\n**Examples (curl):**\n"
        "```bash\n"
        "# Search by keyword\n"
        'curl "http://localhost:8000/ucp/products?q=hoodie&limit=3"\n\n'
        "# Filter by price and stock\n"
        'curl "http://localhost:8000/ucp/products?max_price=50&in_stock=true"\n'
        "```"
    ),
    response_description="List of matching products in UCP format (products, count, limit, offset, has_more).",
    responses={
        200: {
            "description": "Products matching the query",
            "content": {
                "application/json": {
                    "example": {
                        "products": [
                            {
                                "id": "19",
                                "title": "Hoodie with Logo",
                                "short_description": "Soft cotton hoodie with embroidered logo.",
                                "sku": "woo-hoodie-logo",
                                "price": {"amount_micros": 45000000, "currency_code": "USD"},
                                "in_stock": True,
                                "url": "http://localhost:8080/?p=19",
                            },
                            {
                                "id": "23",
                                "title": "Hoodie with Zipper",
                                "short_description": "Zip-up hoodie in multiple colors.",
                                "sku": "woo-hoodie-zip",
                                "price": {"amount_micros": 55000000, "currency_code": "USD"},
                                "in_stock": True,
                                "url": "http://localhost:8080/?p=23",
                            },
                        ],
                        "count": 2,
                        "limit": 20,
                        "offset": 0,
                        "has_more": False,
                    }
                }
            },
        }
    },
)
async def list_products(
    q: Optional[str] = Query(None, description="Keyword search on product title and description."),
    category: Optional[str] = Query(None, description="WooCommerce category slug."),
    min_price: Optional[float] = Query(None, description="Minimum price in USD (e.g. 10.00)."),
    max_price: Optional[float] = Query(None, description="Maximum price in USD (e.g. 49.99)."),
    in_stock: Optional[bool] = Query(None, description="Filter to in-stock products only."),
    limit: int = Query(default=20, ge=1, le=100, description="Max results to return."),
    offset: int = Query(default=0, ge=0, description="Pagination offset."),
    db: AsyncSession = Depends(get_db),
):
    products = await search_products(
        db,
        query=q or "",
        category=category or "",
        min_price=min_price,
        max_price=max_price,
        in_stock=in_stock,
        limit=limit,
        offset=offset,
    )
    return {
        "products": [_format_product(p) for p in products],
        "count": len(products),
        "limit": limit,
        "offset": offset,
        "has_more": len(products) == limit,
    }


@router.get(
    "/products/{product_id}",
    summary="Get a single product",
    description="Retrieve full details for a product by its WooCommerce post ID.",
    responses={
        404: {"description": "Product not found (PRODUCT_NOT_FOUND)", "model": UCPError},
    },
)
async def get_product_detail(product_id: str, db: AsyncSession = Depends(get_db)):
    product = await get_product(db, product_id)
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=UCPError(
                status="not_found",
                messages=[UCPMessage(
                    type="error",
                    code="PRODUCT_NOT_FOUND",
                    content=f"Product '{product_id}' not found.",
                    severity="fatal",
                )],
            ).model_dump(),
        )
    return _format_product(product)
