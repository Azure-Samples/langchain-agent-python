#!/usr/bin/env python3
"""
Database Generation Script for Zava Sales Analysis

This script creates the PostgreSQL database schema and populates it with
product data from the Microsoft AI Tour WRK540 workshop data files.

Usage:
    python generate_database.py

Requirements:
    - POSTGRES_URL environment variable set
    - product_data.json and reference_data.json in data/ folder

Data Files:
    Download from: https://github.com/microsoft/aitour26-WRK540-unlock-your-agents-potential-with-model-context-protocol/tree/main/data/database
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import asyncpg

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def parse_postgres_url(url: str) -> dict:
    """Parse PostgreSQL URL into connection parameters to handle special characters in password."""
    # Parse pattern: postgresql://user:password@host:port/database?params
    match = re.match(
        r"postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/([^?]+)(\?(.+))?", url
    )
    if match:
        user, password, host, port, database, _, params = match.groups()
        result = {
            "user": user,
            "password": password,
            "host": host,
            "port": int(port),
            "database": database,
        }
        # Parse query parameters
        if params:
            for param in params.split("&"):
                if "=" in param:
                    key, value = param.split("=", 1)
                    if key == "sslmode":
                        result["ssl"] = value
        return result
    raise ValueError(f"Invalid PostgreSQL URL format: {url}")


class DatabaseGenerator:
    """Generate and populate Zava sales database."""

    def __init__(self, connection_url: str):
        self.connection_params = parse_postgres_url(connection_url)
        self.conn: asyncpg.Connection = None

    async def create_indexes(self):
        """Create indexes after data is loaded (much faster than before)."""
        logger.info("Creating indexes for query performance...")

        try:
            await self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_products_category 
                ON retail.products(category_id);
            """)

            await self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_products_type 
                ON retail.products(type_id);
            """)

            await self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_image_embeddings_vector 
                ON retail.product_image_embeddings 
                USING ivfflat (image_embedding vector_cosine_ops);
            """)

            await self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_description_embeddings_vector 
                ON retail.product_description_embeddings 
                USING ivfflat (description_embedding vector_cosine_ops);
            """)

            await self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_orders_date 
                ON retail.orders(order_date);
            """)

            await self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_orders_customer 
                ON retail.orders(customer_id);
            """)

            await self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_order_items_order 
                ON retail.order_items(order_id);
            """)

            logger.info("✅ Indexes created successfully")

        except Exception as e:
            logger.error(f"❌ Failed to create indexes: {e}")
            raise

    async def connect(self):
        """Connect to PostgreSQL."""
        try:
            self.conn = await asyncpg.connect(**self.connection_params)
            logger.info("✅ Connected to PostgreSQL")
        except Exception as e:
            logger.error(f"❌ Failed to connect: {e}")
            raise

    async def close(self):
        """Close connection."""
        if self.conn:
            await self.conn.close()
            logger.info("Connection closed")

    async def create_schema(self):
        """Create database schema with pgvector extension."""
        logger.info("Creating database schema...")

        try:
            # Enable pgvector extension
            await self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            logger.info("✅ pgvector extension enabled")

            # Drop and recreate retail schema to start fresh
            await self.conn.execute("DROP SCHEMA IF EXISTS retail CASCADE;")
            await self.conn.execute("CREATE SCHEMA retail;")

            # Set search path to retail schema for this connection
            await self.conn.execute("SET search_path TO retail, public;")
            logger.info("✅ retail schema created (fresh)")

            # Create categories table
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS retail.categories (
                    category_id SERIAL PRIMARY KEY,
                    category_name VARCHAR(100) NOT NULL UNIQUE,
                    description TEXT
                );
            """)

            # Create product types table
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS retail.product_types (
                    type_id SERIAL PRIMARY KEY,
                    type_name VARCHAR(100) NOT NULL,
                    category_id INTEGER REFERENCES retail.categories(category_id),
                    description TEXT,
                    UNIQUE(category_id, type_name)
                );
            """)

            # Create products table
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS retail.products (
                    product_id SERIAL PRIMARY KEY,
                    sku VARCHAR(50) NOT NULL UNIQUE,
                    product_name VARCHAR(200) NOT NULL,
                    product_description TEXT,
                    category_id INTEGER REFERENCES retail.categories(category_id),
                    type_id INTEGER REFERENCES retail.product_types(type_id),
                    cost DECIMAL(10,2),
                    base_price DECIMAL(10,2),
                    gross_margin_percent DECIMAL(5,2)
                );
            """)

            # Create product_image_embeddings table (512-dim)
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS retail.product_image_embeddings (
                    product_id INTEGER PRIMARY KEY REFERENCES retail.products(product_id),
                    image_path VARCHAR(500),
                    image_embedding vector(512)
                );
            """)

            # Create product_description_embeddings table (1536-dim)
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS retail.product_description_embeddings (
                    product_id INTEGER PRIMARY KEY REFERENCES retail.products(product_id),
                    description_embedding vector(1536)
                );
            """)

            # Create stores table
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS retail.stores (
                    store_id SERIAL PRIMARY KEY,
                    store_name VARCHAR(100) NOT NULL UNIQUE,
                    location VARCHAR(200),
                    store_type VARCHAR(50)
                );
            """)

            # Create customers table
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS retail.customers (
                    customer_id SERIAL PRIMARY KEY,
                    customer_name VARCHAR(200),
                    email VARCHAR(200),
                    phone VARCHAR(50),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Create orders table
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS retail.orders (
                    order_id SERIAL PRIMARY KEY,
                    customer_id INTEGER REFERENCES retail.customers(customer_id),
                    store_id INTEGER REFERENCES retail.stores(store_id),
                    order_date TIMESTAMP,
                    total_amount DECIMAL(12,2)
                );
            """)

            # Create order_items table
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS retail.order_items (
                    order_item_id SERIAL PRIMARY KEY,
                    order_id INTEGER REFERENCES retail.orders(order_id),
                    product_id INTEGER REFERENCES retail.products(product_id),
                    quantity INTEGER,
                    unit_price DECIMAL(10,2),
                    discount_percent DECIMAL(5,2)
                );
            """)

            # Create inventory table
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS retail.inventory (
                    inventory_id SERIAL PRIMARY KEY,
                    product_id INTEGER REFERENCES retail.products(product_id),
                    store_id INTEGER REFERENCES retail.stores(store_id),
                    quantity_on_hand INTEGER,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(product_id, store_id)
                );
            """)

            logger.info(
                "✅ Database schema created (indexes will be created after data load)"
            )

        except Exception as e:
            logger.error(f"❌ Failed to create schema: {e}")
            raise

    async def load_product_data(self, product_data: dict):
        """Load products and embeddings from product_data.json."""
        logger.info("Loading product data...")

        try:
            main_categories = product_data.get("main_categories", {})

            # Extract categories from dict keys
            logger.info(f"Found {len(main_categories)} categories")
            for category_name in main_categories.keys():
                await self.conn.execute(
                    """
                    INSERT INTO retail.categories (category_name, description)
                    VALUES ($1, $2)
                    ON CONFLICT (category_name) DO NOTHING
                    """,
                    category_name,
                    "",
                )

            logger.info(f"✅ Categories loaded")

            # Insert product types
            logger.info("Loading product types...")
            for category_name, category_data in main_categories.items():
                # Get category_id
                cat_id = await self.conn.fetchval(
                    "SELECT category_id FROM retail.categories WHERE category_name = $1",
                    category_name,
                )

                for product_type_name in category_data.keys():
                    # Skip seasonal multipliers
                    if product_type_name == "washington_seasonal_multipliers":
                        continue

                    await self.conn.execute(
                        """
                        INSERT INTO retail.product_types (category_id, type_name)
                        VALUES ($1, $2)
                        ON CONFLICT (category_id, type_name) DO NOTHING
                        """,
                        cat_id,
                        product_type_name,
                    )

            logger.info(f"✅ Product types loaded")

            # Extract and load products with embeddings
            product_count = 0
            for category_name, category_data in main_categories.items():
                # Get category_id
                cat_id = await self.conn.fetchval(
                    "SELECT category_id FROM retail.categories WHERE category_name = $1",
                    category_name,
                )

                for product_type_name, products in category_data.items():
                    # Skip seasonal multipliers and non-list values
                    if product_type_name == "washington_seasonal_multipliers":
                        continue
                    if not isinstance(products, list):
                        continue

                    # Get type_id
                    type_id = await self.conn.fetchval(
                        """
                        SELECT type_id FROM retail.product_types 
                        WHERE category_id = $1 AND type_name = $2
                        """,
                        cat_id,
                        product_type_name,
                    )

                    for product in products:
                        if not isinstance(product, dict):
                            continue

                        product_count += 1

                        # Calculate selling price from cost for 33% gross margin
                        cost = float(product.get("price", 0))  # JSON price is the cost
                        base_price = round(
                            cost / 0.67, 2
                        )  # Selling price = Cost / (1 - 0.33)

                        # Insert product
                        product_id = await self.conn.fetchval(
                            """
                            INSERT INTO retail.products (
                                sku, product_name, product_description, 
                                category_id, type_id, cost, base_price, gross_margin_percent
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                            ON CONFLICT (sku) DO UPDATE SET
                                product_name = EXCLUDED.product_name,
                                product_description = EXCLUDED.product_description
                            RETURNING product_id
                            """,
                            product.get("sku", f"SKU-{product_count:06d}"),
                            product.get("name"),
                            product.get("description"),
                            cat_id,
                            type_id,
                            cost,
                            base_price,
                            33.0,
                        )

                        # Insert image embedding if available
                        if "image_embedding" in product and product["image_embedding"]:
                            embedding = product["image_embedding"]
                            if isinstance(embedding, list) and len(embedding) == 512:
                                await self.conn.execute(
                                    """
                                    INSERT INTO retail.product_image_embeddings (product_id, image_path, image_embedding)
                                    VALUES ($1, $2, $3::vector)
                                    ON CONFLICT (product_id) DO UPDATE SET
                                        image_embedding = EXCLUDED.image_embedding,
                                        image_path = EXCLUDED.image_path
                                    """,
                                    product_id,
                                    product.get("image_path", ""),
                                    "[" + ",".join(map(str, embedding)) + "]",
                                )

                        # Insert description embedding if available
                        if (
                            "description_embedding" in product
                            and product["description_embedding"]
                        ):
                            embedding = product["description_embedding"]
                            if isinstance(embedding, list) and len(embedding) == 1536:
                                await self.conn.execute(
                                    """
                                    INSERT INTO retail.product_description_embeddings (product_id, description_embedding)
                                    VALUES ($1, $2::vector)
                                    ON CONFLICT (product_id) DO UPDATE SET
                                        description_embedding = EXCLUDED.description_embedding
                                    """,
                                    product_id,
                                    "[" + ",".join(map(str, embedding)) + "]",
                                )

                    product_count += 1
                    if product_count % 100 == 0:
                        logger.info(f"  Loaded {product_count} products...")

            logger.info(f"✅ Loaded {product_count} products with embeddings")

        except Exception as e:
            logger.error(f"❌ Failed to load product data: {e}")
            raise

    async def load_reference_data(self, reference_data: dict):
        """Load stores and reference data from reference_data.json."""
        logger.info("Loading reference data...")

        try:
            # Load stores - reference_data['stores'] is a dict with store names as keys
            stores = reference_data.get("stores", {})
            for store_name, store_details in stores.items():
                # Extract location from store name (e.g., "Zava Retail Seattle" -> "Seattle")
                location = store_name.replace("Zava Retail ", "").strip()
                if location == "Online":
                    location = "Online"
                # else: City name is already the location from the replace above

                await self.conn.execute(
                    """
                    INSERT INTO retail.stores (store_name, location, store_type)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (store_name) DO NOTHING
                    """,
                    store_name,
                    location,
                    "online" if "Online" in store_name else "physical",
                )

            logger.info(f"✅ Loaded {len(stores)} stores")
            logger.info(f"✅ Reference data loaded")

        except Exception as e:
            logger.error(f"❌ Failed to load reference data: {e}")
            raise

    async def load_products_from_json(self, products_file: Path):
        """Load pre-generated products from JSON file using bulk inserts."""
        logger.info(f"Loading products from {products_file.name}...")

        with open(products_file) as f:
            products_data = json.load(f)

        # Build lookup maps for categories and types
        logger.info("Building category/type lookup maps...")
        category_map = {}
        type_map = {}

        categories = await self.conn.fetch(
            "SELECT category_id, category_name FROM retail.categories"
        )
        for row in categories:
            category_map[row["category_name"]] = row["category_id"]

        types = await self.conn.fetch(
            "SELECT type_id, type_name, category_id FROM retail.product_types"
        )
        for row in types:
            key = (row["category_id"], row["type_name"])
            type_map[key] = row["type_id"]

        # Prepare bulk insert records
        logger.info("Preparing product records...")
        product_records = []
        for p in products_data:
            cat_id = category_map.get(p["category_name"])
            type_id = type_map.get((cat_id, p["type_name"]))
            if cat_id and type_id:
                product_records.append(
                    (
                        p["sku"],
                        p["product_name"],
                        p["product_description"],
                        cat_id,
                        type_id,
                        p["cost"],
                        p["base_price"],
                        p["gross_margin_percent"],
                    )
                )

        # Bulk insert products
        logger.info(f"Bulk inserting {len(product_records)} products...")
        await self.conn.copy_records_to_table(
            "products",
            records=product_records,
            columns=[
                "sku",
                "product_name",
                "product_description",
                "category_id",
                "type_id",
                "cost",
                "base_price",
                "gross_margin_percent",
            ],
        )

        # Get product IDs back (in same order as inserted)
        logger.info("Fetching product IDs...")
        product_ids_rows = await self.conn.fetch(
            "SELECT product_id, sku FROM retail.products ORDER BY product_id DESC LIMIT $1",
            len(product_records),
        )
        product_ids_rows = list(reversed(product_ids_rows))

        # Create SKU to product_id mapping
        sku_to_id = {row["sku"]: row["product_id"] for row in product_ids_rows}

        # Prepare embedding records
        logger.info("Preparing embedding records...")
        image_embedding_records = []
        description_embedding_records = []

        for p in products_data:
            product_id = sku_to_id.get(p["sku"])
            if not product_id:
                continue

            if p.get("image_embedding"):
                image_embedding_records.append(
                    (product_id, p.get("image_path", ""), p["image_embedding"])
                )

            if p.get("description_embedding"):
                description_embedding_records.append(
                    (product_id, p["description_embedding"])
                )

        # Bulk insert image embeddings
        if image_embedding_records:
            logger.info(
                f"Bulk inserting {len(image_embedding_records)} image embeddings..."
            )
            for product_id, image_path, embedding in image_embedding_records:
                await self.conn.execute(
                    "INSERT INTO retail.product_image_embeddings (product_id, image_path, image_embedding) VALUES ($1, $2, $3::vector)",
                    product_id,
                    image_path,
                    "[" + ",".join(map(str, embedding)) + "]",
                )

        # Bulk insert description embeddings
        if description_embedding_records:
            logger.info(
                f"Bulk inserting {len(description_embedding_records)} description embeddings..."
            )
            for product_id, embedding in description_embedding_records:
                await self.conn.execute(
                    "INSERT INTO retail.product_description_embeddings (product_id, description_embedding) VALUES ($1, $2::vector)",
                    product_id,
                    "[" + ",".join(map(str, embedding)) + "]",
                )

        logger.info(
            f"✅ Loaded {len(products_data)} products with embeddings from JSON"
        )

    async def load_customers_from_json(self, customers_file: Path):
        """Load pre-generated customers from JSON file using COPY (fastest method)."""
        logger.info(f"Loading customers from {customers_file.name}...")

        with open(customers_file) as f:
            customers = json.load(f)

        # Use COPY FROM for bulk insert (50-100x faster than individual inserts)
        records = [
            (
                c["customer_name"],
                c["email"],
                c["phone"],
                datetime.fromisoformat(c["created_at"]),
            )
            for c in customers
        ]

        await self.conn.copy_records_to_table(
            "customers",
            records=records,
            columns=["customer_name", "email", "phone", "created_at"],
        )

        logger.info(f"✅ Loaded {len(customers)} customers from JSON")

    async def load_orders_from_json(self, orders_file: Path):
        """Load pre-generated orders and order items from JSON file using batch inserts."""
        logger.info(f"Loading orders from {orders_file.name}...")

        with open(orders_file) as f:
            orders = json.load(f)

        # Prepare all order records for batch insert
        order_records = []
        all_order_items = []

        for order in orders:
            order_records.append(
                (
                    order["customer_id"],
                    order["store_id"],
                    datetime.fromisoformat(order["order_date"]),
                    order["total_amount"],
                )
            )

        # Batch insert all orders using COPY (much faster)
        async with self.conn.transaction():
            # Insert orders and get their IDs
            await self.conn.copy_records_to_table(
                "orders",
                records=order_records,
                columns=["customer_id", "store_id", "order_date", "total_amount"],
            )

            # Get the order IDs that were just inserted (in same order)
            # We need to match them back to the original orders
            order_ids = await self.conn.fetch(
                """
                SELECT order_id, customer_id, store_id, order_date 
                FROM retail.orders 
                ORDER BY order_id DESC 
                LIMIT $1
                """,
                len(orders),
            )

            # Reverse to match original order
            order_ids = list(reversed(order_ids))

            # Build order items with matched order_ids
            for i, order in enumerate(orders):
                order_id = order_ids[i]["order_id"]
                for item in order["items"]:
                    all_order_items.append(
                        (
                            order_id,
                            item["product_id"],
                            item["quantity"],
                            item["unit_price"],
                            item["discount_percent"],
                        )
                    )

            # Batch insert all order items
            if all_order_items:
                await self.conn.copy_records_to_table(
                    "order_items",
                    records=all_order_items,
                    columns=[
                        "order_id",
                        "product_id",
                        "quantity",
                        "unit_price",
                        "discount_percent",
                    ],
                )

        logger.info(
            f"✅ Loaded {len(orders)} orders with {len(all_order_items)} items from JSON"
        )

    async def generate_customers(
        self, num_customers: int = 5000, reference_data: dict = None
    ):
        """Generate synthetic customer records using batch insert."""
        logger.info(f"Generating {num_customers} customers...")

        import random
        from datetime import datetime, timedelta

        first_names = [
            "John",
            "Jane",
            "Michael",
            "Sarah",
            "David",
            "Emily",
            "Robert",
            "Lisa",
            "James",
            "Mary",
            "William",
            "Jennifer",
            "Richard",
            "Linda",
            "Thomas",
            "Patricia",
            "Christopher",
            "Barbara",
            "Daniel",
            "Elizabeth",
            "Matthew",
            "Susan",
        ]
        last_names = [
            "Smith",
            "Johnson",
            "Williams",
            "Brown",
            "Jones",
            "Garcia",
            "Miller",
            "Davis",
            "Rodriguez",
            "Martinez",
            "Hernandez",
            "Lopez",
            "Gonzalez",
            "Wilson",
            "Anderson",
            "Thomas",
            "Taylor",
            "Moore",
            "Jackson",
            "Martin",
        ]

        # Generate all customer records in memory first
        customer_records = []
        for i in range(num_customers):
            first = random.choice(first_names)
            last = random.choice(last_names)
            customer_name = f"{first} {last}"
            email = (
                f"{first.lower()}.{last.lower()}{random.randint(1, 9999)}@example.com"
            )
            phone = f"+1{random.randint(2000000000, 9999999999)}"

            # Random creation date in the past 2 years
            days_ago = random.randint(0, 730)
            created_at = datetime.now() - timedelta(days=days_ago)

            customer_records.append((customer_name, email, phone, created_at))

        # Batch insert all customers at once using COPY
        await self.conn.copy_records_to_table(
            "customers",
            records=customer_records,
            columns=["customer_name", "email", "phone", "created_at"],
        )

        logger.info(f"✅ Generated {num_customers} customers")

    async def generate_orders(
        self, num_orders: int = 10000, reference_data: dict = None
    ):
        """Generate synthetic orders using batch inserts."""
        logger.info(f"Generating {num_orders} orders with items...")

        import random
        from datetime import datetime, timedelta

        # Get stores and their weights
        stores = await self.conn.fetch(
            "SELECT store_id, store_name FROM retail.stores ORDER BY store_id"
        )
        store_weights = {}
        for store in stores:
            store_name = store["store_name"]
            if reference_data and store_name in reference_data.get("stores", {}):
                weight = reference_data["stores"][store_name].get(
                    "customer_distribution_weight", 10
                )
                freq_mult = reference_data["stores"][store_name].get(
                    "order_frequency_multiplier", 1.0
                )
                value_mult = reference_data["stores"][store_name].get(
                    "order_value_multiplier", 1.0
                )
                store_weights[store["store_id"]] = {
                    "weight": weight * freq_mult,
                    "value_multiplier": value_mult,
                }
            else:
                store_weights[store["store_id"]] = {
                    "weight": 10,
                    "value_multiplier": 1.0,
                }

        # Create weighted store list
        weighted_stores = []
        for store in stores:
            weight = int(store_weights[store["store_id"]]["weight"])
            weighted_stores.extend([store] * weight)

        # Get customers and products
        customers = await self.conn.fetch(
            "SELECT customer_id FROM retail.customers ORDER BY customer_id"
        )
        products = await self.conn.fetch(
            "SELECT product_id, base_price, cost FROM retail.products ORDER BY product_id"
        )

        # Generate orders in memory first
        start_date = datetime.now() - timedelta(days=365)
        end_date = datetime.now()
        days_diff = (end_date - start_date).days

        order_records = []
        order_items_list = []  # Will store (order_index, product_id, quantity, unit_price, discount)

        for i in range(num_orders):
            # Random store (weighted)
            store = random.choice(weighted_stores)
            store_id = store["store_id"]
            value_mult = store_weights[store_id]["value_multiplier"]

            # Random customer
            customer = random.choice(customers)
            customer_id = customer["customer_id"]

            # Random order date
            random_days = random.randint(0, days_diff)
            order_date = start_date + timedelta(days=random_days)

            # Number of items (1-5, weighted toward fewer)
            num_items = random.choices([1, 2, 3, 4, 5], weights=[40, 30, 15, 10, 5])[0]

            # Select random products
            order_products = random.sample(products, min(num_items, len(products)))

            # Calculate total and prepare items
            total_amount = 0
            items_for_order = []
            for product in order_products:
                quantity = random.choices([1, 2, 3, 4, 5], weights=[60, 20, 10, 7, 3])[
                    0
                ]
                base_price = float(product["base_price"])
                price_variance = random.uniform(0.95, 1.05)
                unit_price = round(base_price * value_mult * price_variance, 2)
                discount = random.choices(
                    [0, 5, 10, 15, 20], weights=[60, 20, 10, 7, 3]
                )[0]
                item_total = unit_price * quantity * (1 - discount / 100)
                total_amount += item_total

                items_for_order.append(
                    (product["product_id"], quantity, unit_price, discount)
                )

            total_amount = round(total_amount, 2)

            # Store order record
            order_records.append((customer_id, store_id, order_date, total_amount))
            # Store items with order index
            for product_id, quantity, unit_price, discount in items_for_order:
                order_items_list.append((i, product_id, quantity, unit_price, discount))

        # Batch insert all orders and items
        async with self.conn.transaction():
            # Insert all orders
            await self.conn.copy_records_to_table(
                "orders",
                records=order_records,
                columns=["customer_id", "store_id", "order_date", "total_amount"],
            )

            # Get the inserted order IDs (in same order)
            order_ids = await self.conn.fetch(
                """
                SELECT order_id 
                FROM retail.orders 
                ORDER BY order_id DESC 
                LIMIT $1
                """,
                len(order_records),
            )
            order_ids = list(reversed(order_ids))

            # Map order items to actual order IDs
            order_item_records = []
            for (
                order_idx,
                product_id,
                quantity,
                unit_price,
                discount,
            ) in order_items_list:
                order_id = order_ids[order_idx]["order_id"]
                order_item_records.append(
                    (order_id, product_id, quantity, unit_price, discount)
                )

            # Insert all order items
            await self.conn.copy_records_to_table(
                "order_items",
                records=order_item_records,
                columns=[
                    "order_id",
                    "product_id",
                    "quantity",
                    "unit_price",
                    "discount_percent",
                ],
            )

        logger.info(
            f"✅ Generated {num_orders} orders with {len(order_item_records)} items"
        )

    async def generate_inventory(self, reference_data: dict = None):
        """Generate inventory using batch insert."""
        logger.info("Generating inventory data...")

        import random

        stores = await self.conn.fetch("SELECT store_id, store_name FROM retail.stores")
        products = await self.conn.fetch("SELECT product_id FROM retail.products")

        # Generate all inventory records in memory
        inventory_records = []
        now = datetime.now()

        for store in stores:
            for product in products:
                # More inventory for online, less for physical stores
                if "Online" in store["store_name"]:
                    quantity = random.randint(500, 2000)
                else:
                    quantity = random.randint(10, 200)

                inventory_records.append(
                    (product["product_id"], store["store_id"], quantity, now)
                )

        # Batch insert all inventory records
        await self.conn.copy_records_to_table(
            "inventory",
            records=inventory_records,
            columns=["product_id", "store_id", "quantity_on_hand", "last_updated"],
        )

        logger.info(f"✅ Generated {len(inventory_records)} inventory records")


async def main():
    """Main execution function."""
    logger.info("=" * 60)
    logger.info("Zava Sales Database Generator")
    logger.info("=" * 60)

    # Get connection URL
    postgres_url = os.getenv("POSTGRES_URL")
    if not postgres_url:
        logger.error("❌ POSTGRES_URL environment variable not set")
        logger.info(
            "Set it with: export POSTGRES_URL='postgresql://user:pass@host:5432/dbname?sslmode=require'"
        )
        sys.exit(1)

    # Check for pre-generated data files (required)
    data_dir = Path(__file__).parent
    products_json = data_dir / "products_pregenerated.json"
    customers_json = data_dir / "customers_pregenerated.json"
    orders_json = data_dir / "orders_pregenerated.json"

    if not products_json.exists():
        logger.error(f"❌ Products data file not found: {products_json}")
        sys.exit(1)

    if not customers_json.exists():
        logger.error(f"❌ Customers data file not found: {customers_json}")
        sys.exit(1)

    if not orders_json.exists():
        logger.error(f"❌ Orders data file not found: {orders_json}")
        sys.exit(1)

    # Generate database
    generator = DatabaseGenerator(postgres_url)

    try:
        await generator.connect()
        await generator.create_schema()

        # Load products from pre-generated JSON (includes embeddings)
        logger.info("📦 Loading products from pre-generated data...")

        # First, extract and load categories/types from products
        logger.info("Loading products JSON to extract categories and types...")
        with open(products_json) as f:
            products_data = json.load(f)

        # Extract unique categories and types
        categories = {}
        for product in products_data:
            cat_name = product.get("category_name", "UNCATEGORIZED")
            type_name = product.get("type_name", "GENERAL")
            if cat_name not in categories:
                categories[cat_name] = set()
            categories[cat_name].add(type_name)

        # Load categories
        logger.info(f"Loading {len(categories)} categories...")
        for category_name in categories.keys():
            await generator.conn.execute(
                "INSERT INTO retail.categories (category_name, description) VALUES ($1, $2) ON CONFLICT (category_name) DO NOTHING",
                category_name,
                "",
            )

        # Load product types
        logger.info("Loading product types...")
        for category_name, type_names in categories.items():
            cat_id = await generator.conn.fetchval(
                "SELECT category_id FROM retail.categories WHERE category_name = $1",
                category_name,
            )
            for type_name in type_names:
                await generator.conn.execute(
                    "INSERT INTO retail.product_types (category_id, type_name) VALUES ($1, $2) ON CONFLICT (category_id, type_name) DO NOTHING",
                    cat_id,
                    type_name,
                )

        logger.info("✅ Categories and types loaded")

        # Now load products from JSON
        await generator.load_products_from_json(products_json)

        # Create stores for order foreign keys
        # Orders reference store_ids 1-8, so we need to create these stores
        logger.info("Creating stores...")
        store_locations = [
            (1, "Zava Retail Seattle", "Seattle", "physical"),
            (2, "Zava Retail Portland", "Portland", "physical"),
            (3, "Zava Retail San Francisco", "San Francisco", "physical"),
            (4, "Zava Retail Los Angeles", "Los Angeles", "physical"),
            (5, "Zava Retail Denver", "Denver", "physical"),
            (6, "Zava Retail Chicago", "Chicago", "physical"),
            (7, "Zava Retail New York", "New York", "physical"),
            (8, "Zava Online", "Online", "online"),
        ]
        for store_id, store_name, location, store_type in store_locations:
            await generator.conn.execute(
                """
                INSERT INTO retail.stores (store_id, store_name, location, store_type)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (store_id) DO NOTHING
                """,
                store_id,
                store_name,
                location,
                store_type,
            )
        logger.info(f"✅ Created {len(store_locations)} stores")

        # Load customers and orders from pre-generated data
        logger.info("📦 Loading customers and orders from pre-generated data...")
        await generator.load_customers_from_json(customers_json)
        await generator.load_orders_from_json(orders_json)

        await generator.generate_inventory()

        # Create indexes AFTER loading all data (5-10x faster)
        await generator.create_indexes()

        logger.info("=" * 60)
        logger.info("✅ Database generation completed successfully!")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"❌ Database generation failed: {e}")
        sys.exit(1)
    finally:
        await generator.close()


if __name__ == "__main__":
    asyncio.run(main())
