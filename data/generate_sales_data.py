#!/usr/bin/env python3
"""
Generate synthetic sales data for Zava Sales Analysis

This script populates the orders and order_items tables with realistic
sales transactions based on the store distribution weights from reference_data.json
"""
import asyncio
import json
import logging
import os
import random
import re
import sys
from datetime import datetime, timedelta
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


class SalesDataGenerator:
    """Generate synthetic sales data."""
    
    def __init__(self, connection_url: str, reference_data: dict):
        self.connection_params = parse_postgres_url(connection_url)
        self.conn: asyncpg.Connection = None
        self.reference_data = reference_data
        self.stores = []
        self.products = []
        self.customers = []
    
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
    
    async def load_existing_data(self):
        """Load existing stores and products."""
        # Load stores
        self.stores = await self.conn.fetch(
            "SELECT store_id, store_name FROM retail.stores ORDER BY store_id"
        )
        logger.info(f"Loaded {len(self.stores)} stores")
        
        # Load products
        self.products = await self.conn.fetch(
            """
            SELECT product_id, sku, product_name, base_price, cost 
            FROM retail.products 
            ORDER BY product_id
            """
        )
        logger.info(f"Loaded {len(self.products)} products")
    
    async def generate_customers(self, num_customers: int = 5000):
        """Generate synthetic customer records."""
        logger.info(f"Generating {num_customers} customers...")
        
        first_names = ['John', 'Jane', 'Michael', 'Sarah', 'David', 'Emily', 'Robert', 'Lisa', 
                       'James', 'Mary', 'William', 'Jennifer', 'Richard', 'Linda', 'Thomas']
        last_names = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 
                      'Davis', 'Rodriguez', 'Martinez', 'Hernandez', 'Lopez', 'Gonzalez']
        
        for i in range(num_customers):
            first = random.choice(first_names)
            last = random.choice(last_names)
            customer_name = f"{first} {last}"
            email = f"{first.lower()}.{last.lower()}{random.randint(1, 999)}@example.com"
            phone = f"+1{random.randint(2000000000, 9999999999)}"
            
            # Random creation date in the past 2 years
            days_ago = random.randint(0, 730)
            created_at = datetime.now() - timedelta(days=days_ago)
            
            customer_id = await self.conn.fetchval(
                """
                INSERT INTO retail.customers (customer_name, email, phone, created_at)
                VALUES ($1, $2, $3, $4)
                RETURNING customer_id
                """,
                customer_name, email, phone, created_at
            )
            
            if (i + 1) % 1000 == 0:
                logger.info(f"  Generated {i + 1} customers...")
        
        # Reload customers
        self.customers = await self.conn.fetch(
            "SELECT customer_id, customer_name FROM retail.customers ORDER BY customer_id"
        )
        logger.info(f"✅ Generated {len(self.customers)} customers")
    
    async def generate_orders(self, num_orders: int = 20000):
        """Generate synthetic order records with items."""
        logger.info(f"Generating {num_orders} orders with items...")
        
        # Get store weights from reference data
        store_weights = {}
        for store in self.stores:
            store_name = store['store_name']
            if store_name in self.reference_data['stores']:
                weight = self.reference_data['stores'][store_name].get('customer_distribution_weight', 10)
                freq_mult = self.reference_data['stores'][store_name].get('order_frequency_multiplier', 1.0)
                value_mult = self.reference_data['stores'][store_name].get('order_value_multiplier', 1.0)
                store_weights[store['store_id']] = {
                    'weight': weight * freq_mult,
                    'value_multiplier': value_mult
                }
            else:
                store_weights[store['store_id']] = {'weight': 10, 'value_multiplier': 1.0}
        
        # Create weighted store list for random selection
        weighted_stores = []
        for store in self.stores:
            weight = int(store_weights[store['store_id']]['weight'])
            weighted_stores.extend([store] * weight)
        
        # Generate orders over the past year
        start_date = datetime.now() - timedelta(days=365)
        end_date = datetime.now()
        
        for i in range(num_orders):
            # Random store (weighted)
            store = random.choice(weighted_stores)
            store_id = store['store_id']
            value_mult = store_weights[store_id]['value_multiplier']
            
            # Random customer
            customer = random.choice(self.customers)
            customer_id = customer['customer_id']
            
            # Random order date in the past year
            days_diff = (end_date - start_date).days
            random_days = random.randint(0, days_diff)
            order_date = start_date + timedelta(days=random_days)
            
            # Determine number of items (1-5, weighted toward fewer items)
            num_items = random.choices([1, 2, 3, 4, 5], weights=[40, 30, 15, 10, 5])[0]
            
            # Select random products
            order_products = random.sample(self.products, min(num_items, len(self.products)))
            
            # Calculate total
            total_amount = 0
            order_items = []
            
            for product in order_products:
                quantity = random.choices([1, 2, 3, 4, 5], weights=[60, 20, 10, 7, 3])[0]
                
                # Apply value multiplier and some randomness
                base_price = float(product['base_price'])
                price_variance = random.uniform(0.95, 1.05)  # ±5% price variance
                unit_price = round(base_price * value_mult * price_variance, 2)
                
                # Random discount (0%, 5%, 10%, 15%, or 20%)
                discount = random.choices([0, 5, 10, 15, 20], weights=[60, 20, 10, 7, 3])[0]
                
                item_total = unit_price * quantity * (1 - discount / 100)
                total_amount += item_total
                
                order_items.append({
                    'product_id': product['product_id'],
                    'quantity': quantity,
                    'unit_price': unit_price,
                    'discount_percent': discount
                })
            
            total_amount = round(total_amount, 2)
            
            # Insert order
            order_id = await self.conn.fetchval(
                """
                INSERT INTO retail.orders (customer_id, store_id, order_date, total_amount)
                VALUES ($1, $2, $3, $4)
                RETURNING order_id
                """,
                customer_id, store_id, order_date, total_amount
            )
            
            # Insert order items
            for item in order_items:
                await self.conn.execute(
                    """
                    INSERT INTO retail.order_items (order_id, product_id, quantity, unit_price, discount_percent)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    order_id, item['product_id'], item['quantity'], item['unit_price'], item['discount_percent']
                )
            
            if (i + 1) % 1000 == 0:
                logger.info(f"  Generated {i + 1} orders...")
        
        logger.info(f"✅ Generated {num_orders} orders with items")
    
    async def generate_inventory(self):
        """Generate inventory records for all products and stores."""
        logger.info("Generating inventory data...")
        
        count = 0
        for store in self.stores:
            for product in self.products:
                # Random quantity (more for physical stores, less for online)
                if 'Online' in store['store_name']:
                    quantity = random.randint(500, 2000)
                else:
                    quantity = random.randint(10, 200)
                
                await self.conn.execute(
                    """
                    INSERT INTO retail.inventory (product_id, store_id, quantity_on_hand, last_updated)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (product_id, store_id) DO UPDATE SET
                        quantity_on_hand = EXCLUDED.quantity_on_hand,
                        last_updated = EXCLUDED.last_updated
                    """,
                    product['product_id'], store['store_id'], quantity, datetime.now()
                )
                count += 1
        
        logger.info(f"✅ Generated {count} inventory records")


async def main():
    """Main execution function."""
    logger.info("=" * 60)
    logger.info("Zava Sales Data Generator")
    logger.info("=" * 60)
    
    # Get connection URL
    postgres_url = os.getenv('POSTGRES_URL')
    if not postgres_url:
        logger.error("❌ POSTGRES_URL environment variable not set")
        sys.exit(1)
    
    # Load reference data
    data_dir = Path(__file__).parent
    reference_data_file = data_dir / 'reference_data.json'
    
    if not reference_data_file.exists():
        logger.error(f"❌ Reference data file not found: {reference_data_file}")
        sys.exit(1)
    
    logger.info(f"Loading {reference_data_file}...")
    with open(reference_data_file) as f:
        reference_data = json.load(f)
    
    # Generate sales data
    generator = SalesDataGenerator(postgres_url, reference_data)
    
    try:
        await generator.connect()
        await generator.load_existing_data()
        await generator.generate_customers(num_customers=5000)
        await generator.generate_orders(num_orders=20000)
        await generator.generate_inventory()
        
        logger.info("=" * 60)
        logger.info("✅ Sales data generation completed successfully!")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"❌ Sales data generation failed: {e}")
        sys.exit(1)
    finally:
        await generator.close()


if __name__ == '__main__':
    asyncio.run(main())
