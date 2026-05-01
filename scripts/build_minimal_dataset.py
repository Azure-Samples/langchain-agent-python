"""Build a SQLite copy of the Zava sample dataset for the minimal flavor.

Reads the pre-generated JSON files in ``data/`` and produces
``mcp/data/zava.sqlite`` — a single-file database that the MCP server
can use instead of provisioning Postgres + pgvector. Run once after
updating the source JSON files; the resulting ``.sqlite`` is committed
to the repo so end users never run this themselves.

Schema mirrors the Postgres version (see data/generate_database.py) so
that SQL written for one backend works on the other. The 1536-dim
description embeddings are stored as little-endian float32 BLOBs to
keep the file under ~5 MB.

Usage:
    python scripts/build_minimal_dataset.py
"""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
OUT_DIR = REPO_ROOT / "mcp" / "data"
OUT_PATH = OUT_DIR / "zava.sqlite"

PRODUCT_FILE = DATA_DIR / "products_pregenerated.json"
CUSTOMER_FILE = DATA_DIR / "customers_pregenerated.json"
ORDER_FILE = DATA_DIR / "orders_pregenerated.json"
REFERENCE_FILE = DATA_DIR / "reference_data.json"


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS categories (
        category_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        category_name TEXT NOT NULL UNIQUE,
        description   TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS product_types (
        type_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        type_name   TEXT NOT NULL,
        category_id INTEGER NOT NULL REFERENCES categories(category_id),
        description TEXT,
        UNIQUE(category_id, type_name)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS products (
        product_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        sku                   TEXT NOT NULL UNIQUE,
        product_name          TEXT NOT NULL,
        product_description   TEXT,
        category_id           INTEGER REFERENCES categories(category_id),
        type_id               INTEGER REFERENCES product_types(type_id),
        cost                  REAL,
        base_price            REAL,
        gross_margin_percent  REAL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS product_description_embeddings (
        product_id            INTEGER PRIMARY KEY REFERENCES products(product_id),
        description_embedding BLOB NOT NULL  -- 1536 little-endian float32 = 6144 bytes
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS stores (
        store_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        store_name TEXT NOT NULL UNIQUE,
        location   TEXT,
        store_type TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS customers (
        customer_id   INTEGER PRIMARY KEY,
        customer_name TEXT,
        email         TEXT,
        phone         TEXT,
        created_at    TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id  INTEGER REFERENCES customers(customer_id),
        store_id     INTEGER REFERENCES stores(store_id),
        order_date   TEXT,
        total_amount REAL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS order_items (
        order_item_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id         INTEGER REFERENCES orders(order_id),
        product_id       INTEGER REFERENCES products(product_id),
        quantity         INTEGER,
        unit_price       REAL,
        discount_percent REAL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory (
        inventory_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id       INTEGER REFERENCES products(product_id),
        store_id         INTEGER REFERENCES stores(store_id),
        quantity_on_hand INTEGER,
        last_updated     TEXT,
        UNIQUE(product_id, store_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id);",
    "CREATE INDEX IF NOT EXISTS idx_products_type ON products(type_id);",
    "CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(order_date);",
    "CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);",
    "CREATE INDEX IF NOT EXISTS idx_order_items_product ON order_items(product_id);",
]


def encode_embedding(vec: list[float]) -> bytes:
    if len(vec) != 1536:
        raise ValueError(f"expected 1536-dim embedding, got {len(vec)}")
    return struct.pack(f"<{len(vec)}f", *vec)


def main() -> None:
    print(f"Reading from {DATA_DIR}/")
    products = json.loads(PRODUCT_FILE.read_text())
    customers = json.loads(CUSTOMER_FILE.read_text())
    orders = json.loads(ORDER_FILE.read_text())
    reference = json.loads(REFERENCE_FILE.read_text())

    print(
        f"  {len(products)} products | {len(customers)} customers | "
        f"{len(orders)} orders | {len(reference['stores'])} stores"
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if OUT_PATH.exists():
        OUT_PATH.unlink()

    conn = sqlite3.connect(OUT_PATH)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    cur = conn.cursor()

    for stmt in SCHEMA:
        cur.execute(stmt)

    # ---- categories ---------------------------------------------------------
    cat_names: list[str] = []
    seen = set()
    for p in products:
        c = p["category_name"]
        if c not in seen:
            seen.add(c)
            cat_names.append(c)
    cat_id: dict[str, int] = {}
    for c in cat_names:
        cur.execute("INSERT INTO categories (category_name) VALUES (?)", (c,))
        cat_id[c] = cur.lastrowid

    # ---- product_types ------------------------------------------------------
    type_id: dict[tuple[str, str], int] = {}
    for p in products:
        key = (p["category_name"], p["type_name"])
        if key in type_id:
            continue
        cur.execute(
            "INSERT INTO product_types (type_name, category_id) VALUES (?, ?)",
            (p["type_name"], cat_id[p["category_name"]]),
        )
        type_id[key] = cur.lastrowid

    # ---- products + embeddings ---------------------------------------------
    pid_by_sku: dict[str, int] = {}
    for p in products:
        cur.execute(
            """
            INSERT INTO products
              (sku, product_name, product_description, category_id, type_id,
               cost, base_price, gross_margin_percent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                p["sku"],
                p["product_name"],
                p.get("product_description"),
                cat_id[p["category_name"]],
                type_id[(p["category_name"], p["type_name"])],
                p.get("cost"),
                p.get("base_price"),
                p.get("gross_margin_percent"),
            ),
        )
        pid = cur.lastrowid
        pid_by_sku[p["sku"]] = pid

        emb = p.get("description_embedding")
        if emb:
            cur.execute(
                "INSERT INTO product_description_embeddings (product_id, description_embedding) VALUES (?, ?)",
                (pid, encode_embedding(emb)),
            )

    # ---- stores -------------------------------------------------------------
    # reference_data.json maps store_name -> {weights}; ID order is insertion order.
    for store_name in reference["stores"].keys():
        cur.execute("INSERT INTO stores (store_name) VALUES (?)", (store_name,))

    # ---- customers ----------------------------------------------------------
    for c in customers:
        cur.execute(
            "INSERT INTO customers (customer_id, customer_name, email, phone, created_at) VALUES (?, ?, ?, ?, ?)",
            (c["customer_id"], c["customer_name"], c.get("email"), c.get("phone"), c.get("created_at")),
        )

    # ---- orders + order_items ----------------------------------------------
    for o in orders:
        cur.execute(
            "INSERT INTO orders (customer_id, store_id, order_date, total_amount) VALUES (?, ?, ?, ?)",
            (o["customer_id"], o["store_id"], o.get("order_date"), o.get("total_amount")),
        )
        order_id = cur.lastrowid
        for it in o.get("items", []):
            cur.execute(
                """
                INSERT INTO order_items
                  (order_id, product_id, quantity, unit_price, discount_percent)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    it["product_id"],
                    it.get("quantity"),
                    it.get("unit_price"),
                    it.get("discount_percent"),
                ),
            )

    conn.commit()
    cur.execute("VACUUM;")
    conn.close()

    size_mb = OUT_PATH.stat().st_size / 1024 / 1024
    print(f"\n✅ Wrote {OUT_PATH} ({size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
