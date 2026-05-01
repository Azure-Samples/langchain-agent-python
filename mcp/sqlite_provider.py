"""SQLite-backed provider for the MCP server's minimal flavour.

Provides the same external surface as ``PostgreSQLProvider`` (see
``mcp/app.py``) so the tools work unchanged. Tables are exposed under
the ``retail`` schema via ``ATTACH DATABASE`` so SQL written for the
Postgres flavour (``retail.products``, etc.) keeps working.

Embeddings are 1536 little-endian float32 BLOBs. Cosine similarity is
computed in Python with NumPy at query time — fast enough at the
sample-data scale (<1 ms for 424 products).

On ``connect()`` the source database is copied to a writable temp file
and order/customer/inventory dates are shifted forward so that the most
recent ``orders.order_date`` is anchored to today. This keeps queries
like "last quarter" or "this month" working even though the file
shipped in the container image was generated in the past.
"""

from __future__ import annotations

import json
import logging
import shutil
import struct
import tempfile
from datetime import datetime, timezone
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

    def __init__(self, db_path: str, anchor_dates: bool = True):
        self.source_path = str(Path(db_path).resolve())
        # Runtime path; replaced by a writable copy in connect() if anchor_dates.
        self.db_path = self.source_path
        self.anchor_dates = anchor_dates
        self._embedding_matrix: Optional[np.ndarray] = None  # shape (N, 1536), L2-normed
        self._embedding_product_ids: Optional[np.ndarray] = None

    async def _open(self) -> aiosqlite.Connection:
        # Open the database file directly and alias it as "retail" so
        # queries written for Postgres ("FROM retail.products") work as-is.
        # `mode=ro` makes the connection read-only — defence in depth.
        uri = f"file:{self.db_path}?mode=ro"
        conn = await aiosqlite.connect(uri, uri=True)
        conn.row_factory = aiosqlite.Row
        await conn.execute(f"ATTACH DATABASE 'file:{self.db_path}?mode=ro' AS retail")
        return conn

    async def _shift_dates_to_now(self) -> None:
        """Shift order/customer/inventory dates so max(order_date) ≈ today.

        The committed SQLite file was generated against a fixed wall clock,
        so over time its newest order drifts into the past. Shifting once
        at startup preserves the relative spacing between rows while making
        relative-time queries (e.g. "last quarter") naturally return data.
        """
        # Open read-write directly on the runtime copy.
        conn = await aiosqlite.connect(self.db_path)
        try:
            cur = await conn.execute("SELECT MAX(order_date) FROM orders")
            row = await cur.fetchone()
            if not row or not row[0]:
                return
            try:
                # Stored as ISO 8601 without tz; compare in naive UTC.
                max_dt = datetime.fromisoformat(row[0])
            except ValueError:
                logger.warning("Could not parse max order_date %r — skipping shift", row[0])
                return
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            delta_days = (now - max_dt).days
            if delta_days <= 0:
                logger.info("Order dates already current (delta=%dd) — no shift", delta_days)
                return

            modifier = f"+{delta_days} days"
            await conn.execute(
                "UPDATE orders SET order_date = datetime(order_date, ?)", (modifier,)
            )
            await conn.execute(
                "UPDATE customers SET created_at = datetime(created_at, ?)", (modifier,)
            )
            await conn.execute(
                "UPDATE inventory SET last_updated = datetime(last_updated, ?)", (modifier,)
            )
            await conn.commit()
            logger.info(
                "📅 Shifted orders/customers/inventory forward by %d days "
                "(anchor max(order_date) → ~today)",
                delta_days,
            )
        finally:
            await conn.close()

    async def connect(self) -> None:
        if not Path(self.source_path).exists():
            raise FileNotFoundError(f"SQLite database not found at {self.source_path}")

        if self.anchor_dates:
            # Copy to a writable temp path so we can run the date shift.
            # The container image's /app is read-only at runtime in Container Apps,
            # but /tmp is always writable.
            tmp = Path(tempfile.gettempdir()) / "zava-runtime.sqlite"
            shutil.copy2(self.source_path, tmp)
            self.db_path = str(tmp)
            await self._shift_dates_to_now()

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
