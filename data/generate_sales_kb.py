"""Generate the sales-conversation tables (KB articles, case studies, pricing plans, leads).

Additive to `data/generate_database.py` (which sets up products / orders /
embeddings core schema). Creates the new tables if they don't exist and
seeds them with sample rows that match the demo prompts in
`agent/app/prompts/`.

Tables created in the `retail` schema:
  * `pricing_plans` — Starter / Pro / Enterprise tiers.
  * `sales_kb_articles` — short FAQ / how-Zava-works articles, with a
    1536-dim embedding and HNSW index for semantic search.
  * `case_studies` — customer success stories, embedded the same way.
  * `leads` + `lead_activities` — simple CRM table for the agent's
    `update_lead_profile` / `log_activity` writes (left empty at seed time).

Required env:
  * POSTGRES_URL — full connection string.
  * AZURE_OPENAI_ENDPOINT — for embedding generation.
  * AZURE_OPENAI_EMBEDDING_DEPLOYMENT — defaults to `text-embedding-3-small`.

Auth: DefaultAzureCredential (works locally with `az login` and on Azure
with managed identity).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import asyncpg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env.local")
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


# ---- Sample data --------------------------------------------------------
PRICING_PLANS: list[dict[str, Any]] = [
    {
        "plan_name": "Starter",
        "monthly_price": 0,
        "annual_price": 0,
        "features": [
            "Free shop account",
            "Up to 5 saved carts",
            "Standard 2-day shipping",
            "Email-only support",
        ],
    },
    {
        "plan_name": "Pro",
        "monthly_price": 49,
        "annual_price": 490,
        "features": [
            "Everything in Starter",
            "Bulk pricing on 50+ unit orders",
            "Priority same-day shipping",
            "Dedicated chat support",
            "Project budgeting tools",
        ],
    },
    {
        "plan_name": "Enterprise",
        "monthly_price": 299,
        "annual_price": 2990,
        "features": [
            "Everything in Pro",
            "Negotiated trade pricing",
            "Onsite delivery for large orders",
            "Named account executive",
            "SAML SSO + procurement integration",
            "Quarterly business review",
        ],
    },
]


KB_ARTICLES: list[dict[str, Any]] = [
    {
        "title": "How Zava onboards new trade customers",
        "category": "onboarding",
        "body": (
            "Zava's onboarding for trade and pro customers is a 3-step flow: "
            "(1) verify your business via DUNS or state license, "
            "(2) connect your purchasing system (NetSuite, SAP Ariba, Coupa) — "
            "we have a guided wizard that takes ~15 minutes, and "
            "(3) place your first qualifying order to unlock trade pricing. "
            "Most customers are fully onboarded within 1 business day."
        ),
    },
    {
        "title": "Bulk pricing tiers explained",
        "category": "pricing",
        "body": (
            "Zava uses transparent, public bulk pricing tiers: 5% off at 50 units, "
            "10% off at 200 units, 15% off at 500 units. Pro and Enterprise plans "
            "stack on top of these tiers — Enterprise customers can negotiate "
            "fully custom pricing on contract items."
        ),
    },
    {
        "title": "Why pros switch to Zava from big-box stores",
        "category": "competitive",
        "body": (
            "The 3 reasons we hear most: (a) inventory accuracy — our online stock "
            "counts are real-time and reconcile within 5 minutes of every store "
            "transaction, so you don't drive across town to find an empty shelf; "
            "(b) trade pricing without a separate pro app; "
            "(c) named account support for Pro and Enterprise customers — you get a "
            "real person, not a queue."
        ),
    },
    {
        "title": "Same-day delivery coverage",
        "category": "shipping",
        "body": (
            "Same-day delivery is included with the Pro plan in the Seattle, "
            "Tacoma, Spokane, and Bellevue metros, with order-by-noon cutoffs. "
            "Outside those zones, Pro customers get free 2-day shipping; "
            "Enterprise customers can schedule onsite truck delivery for orders "
            "over $5,000."
        ),
    },
    {
        "title": "How returns and exchanges work for pros",
        "category": "returns",
        "body": (
            "Pro customers get a 90-day return window on most items (vs 30 days "
            "on Starter). Returns can be initiated in-app or by replying to your "
            "order confirmation email. We pay return shipping for any defective "
            "or mis-picked items. Returns are typically refunded within 3 business "
            "days of pickup."
        ),
    },
]


CASE_STUDIES: list[dict[str, Any]] = [
    {
        "title": "Acme Construction cut tooling spend 18% in their first year",
        "industry": "construction",
        "team_size": 45,
        "summary": (
            "Acme Construction (45 trades, Seattle metro) switched from a big-box "
            "supplier to Zava Pro. They consolidated 3 supplier accounts, plugged "
            "Zava into their NetSuite procurement flow, and used Zava's bulk "
            "pricing tiers on hardware. Result: 18% reduction in tooling and "
            "consumables spend over 12 months, plus a measurable drop in 'truck "
            "trips to find missing items'."
        ),
    },
    {
        "title": "Cascade Manufacturing standardised on Zava Enterprise",
        "industry": "manufacturing",
        "team_size": 220,
        "summary": (
            "Cascade Manufacturing rolled out Zava Enterprise across 4 plants. "
            "They use the SAML SSO integration and procurement passthrough "
            "from SAP Ariba. Their named AE runs a quarterly business review where "
            "they renegotiate contract pricing on top 100 SKUs."
        ),
    },
    {
        "title": "Northshore Property Management — fewer 'urgent runs', faster turnovers",
        "industry": "property_management",
        "team_size": 25,
        "summary": (
            "Northshore manages ~600 residential units. Their maintenance "
            "supervisors use Zava's same-day delivery in the Seattle metro to "
            "skip emergency hardware-store runs entirely. Average unit-turnover "
            "time dropped from 6 days to 4."
        ),
    },
    {
        "title": "Riveter Workshop — a small custom shop on Zava Pro",
        "industry": "manufacturing",
        "team_size": 6,
        "summary": (
            "Riveter Workshop is a 6-person custom-furniture shop. They moved off "
            "their previous big-box trade card to Zava Pro and use the project "
            "budgeting tools to track per-job material spend. Their finishing-"
            "supplies cost-per-job dropped 12% as a side effect of the better "
            "visibility."
        ),
    },
    {
        "title": "Pierce County Schools standardised on Zava for shop-class supply",
        "industry": "education",
        "team_size": 80,
        "summary": (
            "Pierce County Schools uses Zava Enterprise for shop-class consumables "
            "across 12 high schools. They run a quarterly bulk replenishment cycle "
            "via the procurement integration; Zava handles per-school delivery and "
            "consolidated billing back to the district."
        ),
    },
]


# ---- DB helpers ---------------------------------------------------------
_DSN_RE = re.compile(r"^([^:]+://)([^:/@]+):([^@]+)@(.+)$")


def _safe_dsn(url: str) -> str:
    m = _DSN_RE.match(url)
    if not m:
        return url
    scheme, user, password, rest = m.groups()
    return f"{scheme}{quote(user, safe='')}:{quote(password, safe='')}@{rest}"


CREATE_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS retail;

CREATE TABLE IF NOT EXISTS retail.pricing_plans (
    plan_id        SERIAL PRIMARY KEY,
    plan_name      TEXT UNIQUE NOT NULL,
    monthly_price  NUMERIC(10,2),
    annual_price   NUMERIC(10,2),
    features       JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS retail.sales_kb_articles (
    article_id     SERIAL PRIMARY KEY,
    title          TEXT NOT NULL,
    category       TEXT NOT NULL,
    body           TEXT NOT NULL,
    embedding      vector(1536),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS retail.case_studies (
    case_id        SERIAL PRIMARY KEY,
    title          TEXT NOT NULL,
    industry       TEXT NOT NULL,
    team_size      INTEGER NOT NULL,
    summary        TEXT NOT NULL,
    embedding      vector(1536),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS retail.leads (
    lead_id        SERIAL PRIMARY KEY,
    email          TEXT UNIQUE,
    company_name   TEXT,
    industry       TEXT,
    team_size      INTEGER,
    budget         TEXT,
    authority      TEXT,
    timeline       TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS retail.lead_activities (
    activity_id    SERIAL PRIMARY KEY,
    lead_id        INTEGER REFERENCES retail.leads(lead_id) ON DELETE CASCADE,
    activity_type  TEXT NOT NULL,
    note           TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# HNSW indexes are created as separate statements so we can ignore errors on
# Postgres builds without HNSW support (older pgvector). IVFFLAT also works
# but HNSW gives better recall at our scale.
HNSW_INDEXES = [
    "CREATE INDEX IF NOT EXISTS sales_kb_articles_embedding_hnsw "
    "ON retail.sales_kb_articles USING hnsw (embedding vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64);",
    "CREATE INDEX IF NOT EXISTS case_studies_embedding_hnsw "
    "ON retail.case_studies USING hnsw (embedding vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64);",
]


async def _embed(client: AsyncAzureOpenAI, deployment: str, texts: list[str]) -> list[list[float]]:
    resp = await client.embeddings.create(model=deployment, input=texts)
    return [d.embedding for d in resp.data]


async def main() -> None:
    postgres_url = os.environ.get("POSTGRES_URL")
    if not postgres_url:
        raise SystemExit("POSTGRES_URL is required.")
    aoai_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    if not aoai_endpoint:
        raise SystemExit("AZURE_OPENAI_ENDPOINT is required.")
    deployment = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

    credential = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default"
    )
    aoai = AsyncAzureOpenAI(
        api_version=api_version,
        azure_endpoint=aoai_endpoint.rstrip("/"),
        azure_ad_token_provider=token_provider,
    )

    pool = await asyncpg.create_pool(dsn=_safe_dsn(postgres_url), min_size=1, max_size=4)
    try:
        async with pool.acquire() as conn:
            logger.info("Creating sales tables (idempotent)...")
            await conn.execute(CREATE_SQL)
            for stmt in HNSW_INDEXES:
                try:
                    await conn.execute(stmt)
                except Exception as exc:
                    logger.warning("Skipping HNSW index: %s", exc)

            # ---- Pricing plans (no embeddings) ----------------------------
            for plan in PRICING_PLANS:
                await conn.execute(
                    """
                    INSERT INTO retail.pricing_plans (plan_name, monthly_price, annual_price, features)
                    VALUES ($1, $2, $3, $4::jsonb)
                    ON CONFLICT (plan_name) DO UPDATE
                       SET monthly_price = EXCLUDED.monthly_price,
                           annual_price  = EXCLUDED.annual_price,
                           features      = EXCLUDED.features;
                    """,
                    plan["plan_name"], plan["monthly_price"], plan["annual_price"],
                    json.dumps(plan["features"]),
                )

            # ---- KB articles (with embeddings) ----------------------------
            kb_existing = await conn.fetchval("SELECT COUNT(*) FROM retail.sales_kb_articles;")
            if kb_existing == 0:
                logger.info("Seeding %d KB articles...", len(KB_ARTICLES))
                texts = [f"{a['title']}\n\n{a['body']}" for a in KB_ARTICLES]
                embeddings = await _embed(aoai, deployment, texts)
                for a, vec in zip(KB_ARTICLES, embeddings):
                    pg_vec = "[" + ",".join(map(str, vec)) + "]"
                    await conn.execute(
                        "INSERT INTO retail.sales_kb_articles (title, category, body, embedding) "
                        "VALUES ($1, $2, $3, $4::vector);",
                        a["title"], a["category"], a["body"], pg_vec,
                    )
            else:
                logger.info("KB articles already present (%d rows) — skipping seed.", kb_existing)

            # ---- Case studies (with embeddings) --------------------------
            cs_existing = await conn.fetchval("SELECT COUNT(*) FROM retail.case_studies;")
            if cs_existing == 0:
                logger.info("Seeding %d case studies...", len(CASE_STUDIES))
                texts = [f"{c['title']}\n\n{c['summary']}" for c in CASE_STUDIES]
                embeddings = await _embed(aoai, deployment, texts)
                for c, vec in zip(CASE_STUDIES, embeddings):
                    pg_vec = "[" + ",".join(map(str, vec)) + "]"
                    await conn.execute(
                        "INSERT INTO retail.case_studies (title, industry, team_size, summary, embedding) "
                        "VALUES ($1, $2, $3, $4, $5::vector);",
                        c["title"], c["industry"], c["team_size"], c["summary"], pg_vec,
                    )
            else:
                logger.info("Case studies already present (%d rows) — skipping seed.", cs_existing)

        logger.info("✅ Sales schema + seed data ready.")
    finally:
        await pool.close()
        await aoai.close()
        await credential.close()


if __name__ == "__main__":
    asyncio.run(main())
