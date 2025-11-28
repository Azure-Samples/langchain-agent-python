#!/usr/bin/env python3
"""
Export existing sales data from PostgreSQL to JSON files.
This creates pre-generated data files that can be loaded instantly.

Usage:
    python export_sales_data.py

Requirements:
    - POSTGRES_URL environment variable set
"""

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

import asyncpg

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def parse_postgres_url(url: str) -> dict:
    """Parse PostgreSQL URL into connection parameters."""
    match = re.match(r'postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/([^?]+)(\?(.+))?', url)
    if match:
        user, password, host, port, database, _, params = match.groups()
        result = {
            'user': user,
            'password': password,
            'host': host,
            'port': int(port),
            'database': database
        }
        if params:
            for param in params.split('&'):
                if '=' in param:
                    key, value = param.split('=', 1)
                    if key == 'sslmode':
                        result['ssl'] = value
        return result
    raise ValueError(f"Invalid PostgreSQL URL format: {url}")


async def export_data():
    """Export customers and orders to JSON files."""
    
    # Get connection URL
    postgres_url = os.getenv('POSTGRES_URL')
    if not postgres_url:
        logger.error("❌ POSTGRES_URL environment variable not set")
        sys.exit(1)
    
    connection_params = parse_postgres_url(postgres_url)
    
    logger.info("=" * 60)
    logger.info("Exporting Sales Data to JSON")
    logger.info("=" * 60)
    
    conn = None
    try:
        # Connect to database
        conn = await asyncpg.connect(**connection_params)
        logger.info("✅ Connected to PostgreSQL")
        
        # Export customers
        logger.info("Exporting customers...")
        customers_rows = await conn.fetch("""
            SELECT customer_id, customer_name, email, phone, created_at
            FROM retail.customers
            ORDER BY customer_id
        """)
        
        customers = []
        for row in customers_rows:
            customers.append({
                'customer_id': row['customer_id'],
                'customer_name': row['customer_name'],
                'email': row['email'],
                'phone': row['phone'],
                'created_at': row['created_at'].isoformat()
            })
        
        logger.info(f"✅ Exported {len(customers)} customers")
        
        # Export orders with items
        logger.info("Exporting orders...")
        orders_rows = await conn.fetch("""
            SELECT order_id, customer_id, store_id, order_date, total_amount
            FROM retail.orders
            ORDER BY order_id
        """)
        
        orders = []
        for order_row in orders_rows:
            # Get order items
            items_rows = await conn.fetch("""
                SELECT product_id, quantity, unit_price, discount_percent
                FROM retail.order_items
                WHERE order_id = $1
                ORDER BY order_item_id
            """, order_row['order_id'])
            
            items = []
            for item_row in items_rows:
                items.append({
                    'product_id': item_row['product_id'],
                    'quantity': item_row['quantity'],
                    'unit_price': float(item_row['unit_price']),
                    'discount_percent': float(item_row['discount_percent'])
                })
            
            orders.append({
                'customer_id': order_row['customer_id'],
                'store_id': order_row['store_id'],
                'order_date': order_row['order_date'].isoformat(),
                'total_amount': float(order_row['total_amount']),
                'items': items
            })
        
        logger.info(f"✅ Exported {len(orders)} orders")
        
        # Save to files
        data_dir = Path(__file__).parent
        customers_file = data_dir / 'customers_pregenerated.json'
        orders_file = data_dir / 'orders_pregenerated.json'
        
        logger.info(f"Writing {customers_file}...")
        with open(customers_file, 'w') as f:
            json.dump(customers, f, indent=2)
        
        logger.info(f"Writing {orders_file}...")
        with open(orders_file, 'w') as f:
            json.dump(orders, f, indent=2)
        
        # Get file sizes
        customers_size = customers_file.stat().st_size / 1024 / 1024
        orders_size = orders_file.stat().st_size / 1024 / 1024
        
        logger.info("=" * 60)
        logger.info("✅ Export completed successfully!")
        logger.info(f"📦 customers_pregenerated.json: {customers_size:.2f} MB")
        logger.info(f"📦 orders_pregenerated.json: {orders_size:.2f} MB")
        logger.info(f"📦 Total: {customers_size + orders_size:.2f} MB")
        logger.info("=" * 60)
        logger.info("")
        logger.info("These files can now be committed to the repository.")
        logger.info("Next time generate_database.py runs, it will auto-detect")
        logger.info("and load from these files for instant setup!")
        
    except Exception as e:
        logger.error(f"❌ Export failed: {e}")
        sys.exit(1)
    finally:
        if conn:
            await conn.close()
            logger.info("Connection closed")


if __name__ == '__main__':
    asyncio.run(export_data())
