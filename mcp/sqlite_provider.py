"""SQLite-backed provider for the MCP server's minimal flavour.

Provides the same external surface as ``PostgreSQLProvider`` (see
``mcp/app.py``) so the tools work unchanged. Tables are exposed under
the ``retail`` schema via ``ATTACH DATABASE`` so SQL written for the
Postgres flavour (``retail.products``, etc.) keeps working.

Embeddings are 1536 little-endian float32 BLOBs. Cosine similarity is
computed in Python with NumPy at query time — fast enough at the
sample-data scale (<1 ms for 424 products).
"""

from __future__ import annotations

import json
import logging
import struct
from pathlib import Path
from typing import Optional

import aiosqlite
import numpy as np
from fastmcp import Context
from fastmcp.exceptions import ToolError
from openai import AsyncAzureOpenAI

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1536


def _decode_embedding(blob: bytes) -> np.ndarray:
    n = len(blob) // 4
    return np.array(struct.unpack(f"<{n}f", blob), dtype=np.float32)


class SQLiteProvider:
    """Read-only async SQLite wrapper that mirrors the PostgreSQLProvider API."""

    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path).resolve())
        self._embedding_matrix: Optional[np.ndarray] = None  # shape (N, 1536), L2-normed
        self._embedding_product_ids: Optional[np.ndarray] = None

    async def _open(self) -> aiosqlite.Connection:
        # Open the database file directly and alias it as "retail" so
        # queries written for Postgres ("FROM retail.products") work as-is.
        # `mode=ro` makes the connection read-only — defence in depth.
        uri = f"file:{self.db_path}?mode=ro"
        conn = await aiosqlite.connect(uri, uri=True)
        conn.row_factory = aiosqlite.Row
        # Attach the same file under the alias "retail"; SQLite is happy to
        # have the same DB attached under multiple names.
        await conn.execute(f"ATTACH DATABASE 'file:{self.db_path}?mode=ro' AS retail")
        return conn

    async def connect(self) -> None:
        if not Path(self.db_path).exists():
            raise FileNotFoundError(f"SQLite database not found at {self.db_path}")

        # Pre-load embeddings into memory once so semantic search is ~free.
        conn = await self._open()
        try:
            cur = await conn.execute(
                "SELECT product_id, description_embedding FROM retail.product_description_embeddings"
            )
            rows = await cur.fetchall()
        finally:
            await conn.close()

        if rows:
            ids = np.array([r["product_id"] for r in rows], dtype=np.int64)
            mat = np.stack([_decode_embedding(r["description_embedding"]) for r in rows])
            # L2-normalise once so cosine similarity becomes a simple dot product.
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._embedding_matrix = mat / norms
            self._embedding_product_ids = ids
            logger.info("✅ SQLite ready: %s (%d embeddings preloaded)", self.db_path, len(ids))
        else:
            logger.warning("SQLite at %s has no embeddings", self.db_path)

    async def close(self) -> None:
        # No persistent connection to close — each query opens a fresh one.
        return None

    async def execute_query(self, query: str) -> list[dict]:
        conn = await self._open()
        try:
            cur = await conn.execute(query)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            await conn.close()

    async def get_table_schemas(self) -> str:
        """Return JSON describing every table in the SQLite db.

        Output shape matches PostgreSQLProvider.get_table_schemas() so the
        agent's prompt does not need backend-specific awareness.
        """
        conn = await self._open()
        try:
            cur = await conn.execute(
                "SELECT name FROM retail.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            tables = [r["name"] for r in await cur.fetchall()]

            out: dict[str, list[dict]] = {}
            for t in tables:
                cur = await conn.execute(f"PRAGMA retail.table_info({t})")
                cols = await cur.fetchall()
                out[t] = [
                    {
                        "column": c["name"],
                        "type": c["type"].lower() if c["type"] else "text",
                        "nullable": not bool(c["notnull"]),
                        "default": c["dflt_value"],
                    }
                    for c in cols
                ]
        finally:
            await conn.close()
        return json.dumps(out, indent=2)

    async def search_products(
        self,
        embed_client: AsyncAzureOpenAI,
        embedding_deployment: str,
        query: str,
        max_rows: int = 5,
        threshold: float = 0.7,
        ctx: Optional[Context] = None,
    ) -> str:
        if self._embedding_matrix is None or self._embedding_product_ids is None:
            raise ToolError("Embeddings not loaded; database may be empty.")

        if ctx:
            await ctx.report_progress(progress=1, total=3)
            await ctx.info(f"Getting embedding for query: {query[:50]}...")

        resp = await embed_client.embeddings.create(
            input=query, model=embedding_deployment
        )
        q = np.array(resp.data[0].embedding, dtype=np.float32)
        n = np.linalg.norm(q)
        if n > 0:
            q = q / n

        if ctx:
            await ctx.report_progress(progress=2, total=3)
            await ctx.info("Searching products in database...")

        sims = self._embedding_matrix @ q  # (N,)
        idx = np.argsort(-sims)
        keep = [(int(self._embedding_product_ids[i]), float(sims[i])) for i in idx if sims[i] > threshold]
        keep = keep[:max_rows]

        if ctx:
            await ctx.report_progress(progress=3, total=3)

        if not keep:
            return f"No products found matching '{query}' with similarity > {threshold}"

        # Look up the matching product rows in one query.
        ids_csv = ",".join(str(pid) for pid, _ in keep)
        conn = await self._open()
        try:
            cur = await conn.execute(
                f"""
                SELECT p.product_id, p.product_name, p.product_description,
                       c.category_name, p.base_price
                FROM retail.products p
                JOIN retail.categories c ON c.category_id = p.category_id
                WHERE p.product_id IN ({ids_csv})
                """
            )
            rows = {r["product_id"]: dict(r) for r in await cur.fetchall()}
        finally:
            await conn.close()

        lines = []
        for pid, sim in keep:
            r = rows.get(pid)
            if not r:
                continue
            desc = (r.get("product_description") or "")[:100]
            lines.append(
                f"• {r['product_name']} ({r['category_name']}) - "
                f"${r['base_price']:.2f} - Similarity: {sim:.2%}\n  {desc}..."
            )
        return "\n\n".join(lines)
