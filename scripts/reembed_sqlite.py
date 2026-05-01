"""Regenerate description embeddings in mcp/data/zava.sqlite using the live
Azure OpenAI embedding deployment.

Why: the embeddings shipped in data/products_pregenerated.json are from a
different model than what Bicep deploys (text-embedding-ada-002), so cosine
similarity is near zero. This script rebuilds them so semantic search works
out of the box on the SQLite (minimal) flavor.

Run once (or whenever the embedding model changes); commit the updated
zava.sqlite. Requires AZURE_OPENAI_ENDPOINT in env (and Entra ID auth).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import struct
import sys
from pathlib import Path

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "mcp" / "data" / "zava.sqlite"
BATCH = 16


async def main() -> int:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    if not endpoint:
        log.error("AZURE_OPENAI_ENDPOINT not set. Run `azd env get-values` first.")
        return 1
    deployment = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-ada-002")
    log.info("Using endpoint=%s deployment=%s", endpoint, deployment)

    cred = DefaultAzureCredential()
    tok = get_bearer_token_provider(cred, "https://cognitiveservices.azure.com/.default")
    client = AsyncAzureOpenAI(
        api_version="2024-10-21",
        azure_endpoint=endpoint,
        azure_ad_token_provider=tok,
    )

    # Open SQLite read-write; pull (id, description) for all products.
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT product_id, product_description FROM products ORDER BY product_id"
    ).fetchall()
    log.info("Re-embedding %d products...", len(rows))

    updates: list[tuple[bytes, int]] = []
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        texts = [r["product_description"] or "" for r in chunk]
        resp = await client.embeddings.create(input=texts, model=deployment)
        for r, item in zip(chunk, resp.data):
            vec = item.embedding
            blob = struct.pack(f"<{len(vec)}f", *vec)
            updates.append((blob, r["product_id"]))
        log.info("  embedded %d/%d", min(i + BATCH, len(rows)), len(rows))

    con.executemany(
        "UPDATE product_description_embeddings SET description_embedding = ? WHERE product_id = ?",
        updates,
    )
    con.commit()
    con.close()
    log.info("✅ Updated %d rows in %s", len(updates), DB_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
