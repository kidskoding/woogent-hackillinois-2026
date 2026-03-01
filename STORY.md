# The Story of Woogent

---

## The problem

> Retail isn’t one market: it’s **three**

| Segment | Who they are | How they sell online |
|--------|----------------|-----------------------|
| **Large enterprises** | Amazon, **Walmart**, **Target** | Their own platforms, huge engineering teams, and custom agentic commerce |
| **Medium retailers** | Brands on **Shopify**, **Wix**, **Etsy** | Marketplaces with APIs; vendors are adding AI/agent support |
| **Small retailers** | ~**4 million** stores | WooCommerce, Magento: free and open source with no AI agent integration

The first two are getting AI storefronts, voice shopping, and agent integrations. The third is stuck. They don’t have the budget for a custom API, the headcount to integrate with every new AI product, or the time to migrate off the stack that already works for them.

So when we talk about agentic commerce, we're really talking about a split, where stores with a path to AI are not the same as stores without one

Roughly **4 million small retailers** sit on the wrong side of that line. That’s what inspired Woogent to bring a **standards-based agent API** backed by **UCP** to the stack these small retailers already use, WooCommerce, so that they don’t have to rebuild or pay for a new platform.

---

## What I learned

- **Universal Commerce Protocol (UCP)**  
  I went deep on the [UCP spec](https://ucp.dev): discovery at `/.well-known/ucp`, REST semantics for products and checkout sessions, and the state machine (e.g. `incomplete → pending_payment → completed`). Learning how Google and others expect agents to discover and call commerce APIs shaped the whole design.

- **WooCommerce under the hood**  
  WooCommerce is WordPress + a lot of meta and custom tables. Products live in `wp_posts` + `wp_postmeta`; orders moved to **HPOS** (`wc_orders`, `wc_order_addresses`, etc.), not `wp_posts`. I learned to read the schema, write raw SQL for products and orders, and avoid assuming “everything is a post.”

- **OAuth 2.0 + JWTs for agents**  
  Agents need a token to act on behalf of a user. I implemented the Authorization Code grant, HTTP Basic at the token endpoint, and RS256 JWTs so the API can verify callers without storing sessions. The public key lives at `/.well-known/jwks.json` so any client can validate tokens.

- **Gemini function calling**  
  I modeled the demo as “Gemini + a set of tools that map to UCP endpoints.” Designing the tool schemas so Gemini could reliably choose the right call (e.g. search products vs. create session vs. complete) took iteration; clear names and parameter descriptions mattered a lot.

- **Stripe PaymentIntents**  
  I wired checkout to Stripe’s API (create PaymentIntent, confirm with a test token) so the flow is real end-to-end, and stored the transaction ID on the WooCommerce order for traceability.

---

## How I built it

1. **API-first**  
   I started with the UCP surface: discovery, products, checkout sessions, orders. FastAPI + Pydantic gave me OpenAPI docs and validation; every route returns UCP-shaped JSON and UCP-style errors.

2. **WooCommerce as the source of truth**  
   No duplicate product or order store. The API reads and writes the same MySQL DB that WordPress uses. That keeps the demo honest: “this is what a small merchant would run.”

3. **Layered services**  
   Routes stay thin; business logic lives in `ucp_adapter` (session lifecycle, order creation, shipping options). Auth is in `auth` (OAuth, JWT, key generation). DB access is in `woo_queries` (raw SQL). That made it easier to test and to reason about each piece.

4. **Demo as a client**  
   The Gradio + Gemini app is just one client of the API. It discovers the store, searches products, and drives checkout by calling the same endpoints that cURL or Postman would. That keeps the API the product and the demo a proof.

5. **Docker Compose for one-command run**  
   MySQL, WordPress (with WooCommerce + sample data), the API, and the demo all start with `docker compose up -d`. A `wpcli` job handles WooCommerce setup and sample import so judges (or you) can run it locally without touching WordPress manually.

---

## Challenges I faced

- **WooCommerce’s dual world**  
   Products are in `wp_posts`; orders are in HPOS tables. I had to map UCP concepts (line items, totals, addresses) onto both schemas and handle the fact that order IDs are no longer post IDs. A lot of debugging was “is this coming from `wp_postmeta` or `wc_order_addresses`?”

- **Idempotency and headers**  
   UCP expects `Idempotency-Key`, `Request-Id`, and `UCP-Agent` on state-changing calls. Implementing and testing idempotent create/update/complete so the same key doesn’t double-create orders took care; I used an in-memory store for the hackathon and would move to Redis or DB for production.

- **Gemini doing the right thing at the right time**  
   The model had to discover the store first, then search, then create a session, then set address, then complete. Sometimes it tried to complete before setting shipping or used the wrong product ID. I improved this by tightening the tool descriptions and by including minimal “next step” hints in the system prompt so the flow stays on track.

- **Production parity**  
   Getting the same stack running on a VPS (WordPress URL, OAuth redirects, Stripe webhooks, product images) meant fixing `WP_SITEURL`, CORS, and asset paths. I added a “refresh products” path so prod could re-import sample data and descriptions without wiping the server.

- **Time box**  
   In a weekend you can’t do everything. I focused on: UCP compliance, a working checkout with Stripe, and a Gemini demo that could run end-to-end. Things like webhook signing, retries, and a full order-history UI stayed as “next steps.”

---

## Why did I build Woogent?

Woogent doesn’t ask small retailers to leave WooCommerce or to hire an eng team (both of which come with real risk and cost). Instead, it acts as a **bridge**: a UCP-backed API that allows any agent or integration to call it so that the same stores that already run on WordPress and WooCommerce can show up in the next wave of AI shopping, joining **Amazon** and **Shopify**!
