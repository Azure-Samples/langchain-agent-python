#!/usr/bin/env python3
"""
Regenerate product description embeddings using the current Azure OpenAI embedding model.

This script updates the embeddings in the database to match the model being used for queries.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import asyncpg
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

# Load environment variables
from dotenv import load_dotenv
from openai import AsyncAzureOpenAI

env_path = Path(__file__).parent.parent / ".env.local"
load_dotenv(env_path)

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def regenerate_embeddings():
    """Regenerate all product description embeddings."""

    # Check required environment variables
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    if not endpoint:
        logger.error("❌ AZURE_OPENAI_ENDPOINT not set")
        sys.exit(1)

    embedding_model = os.environ.get(
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"
    )
    logger.info(f"Using endpoint: {endpoint}")
    logger.info(f"Using embedding model: {embedding_model}")

    # Initialize Azure OpenAI client
    credential = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default"
    )

    client = AsyncAzureOpenAI(
        api_version="2024-10-21",
        azure_endpoint=endpoint,
        azure_ad_token_provider=token_provider,
    )

    # Connect to database
    logger.info("Connecting to database...")
    conn = await asyncpg.connect(
        user="postgres",
        password="postgres",
        host="localhost",
        port=5432,
        database="zava",
    )

    # Get all products with their descriptions
    logger.info("Fetching products...")
    products = await conn.fetch("""
        SELECT product_id, product_name, product_description 
        FROM retail.products 
        ORDER BY product_id
    """)

    logger.info(f"Found {len(products)} products to process")

    # Process in batches of 20 (Azure OpenAI limit)
    batch_size = 20
    updated = 0

    for i in range(0, len(products), batch_size):
        batch = products[i : i + batch_size]

        # Prepare texts for embedding
        texts = []
        product_ids = []
        for p in batch:
            # Combine name and description for better semantic matching
            text = f"{p['product_name']}: {p['product_description'] or ''}"
            texts.append(text)
            product_ids.append(p["product_id"])

        # Get embeddings for batch
        try:
            response = await client.embeddings.create(
                input=texts, model=embedding_model
            )

            # Update each product's embedding
            for j, embedding_data in enumerate(response.data):
                embedding = embedding_data.embedding
                product_id = product_ids[j]

                embedding_str = "[" + ",".join(map(str, embedding)) + "]"

                # Update or insert the embedding
                await conn.execute(
                    """
                    INSERT INTO retail.product_description_embeddings (product_id, description_embedding)
                    VALUES ($1, $2::vector)
                    ON CONFLICT (product_id) DO UPDATE SET
                        description_embedding = EXCLUDED.description_embedding
                """,
                    product_id,
                    embedding_str,
                )

                updated += 1

            logger.info(f"Processed {updated}/{len(products)} products...")

        except Exception as e:
            logger.error(f"Error processing batch: {e}")
            raise

    await conn.close()
    logger.info(f"✅ Successfully regenerated {updated} embeddings!")

    # Verify by testing a search
    logger.info("\nVerifying with test search for 'hammers'...")
    conn = await asyncpg.connect(
        user="postgres",
        password="postgres",
        host="localhost",
        port=5432,
        database="zava",
    )

    # Get embedding for 'hammers'
    response = await client.embeddings.create(input="hammers", model=embedding_model)
    query_embedding = response.data[0].embedding
    embedding_str = "[" + ",".join(map(str, query_embedding)) + "]"

    results = await conn.fetch(
        """
        SELECT p.product_name, 1 - (de.description_embedding <=> $1::vector) as similarity 
        FROM retail.products p 
        JOIN retail.product_description_embeddings de ON p.product_id = de.product_id 
        ORDER BY similarity DESC
        LIMIT 10;
    """,
        embedding_str,
    )

    logger.info("Top 10 products by similarity to 'hammers':")
    for row in results:
        logger.info(f"  {row['similarity']:.4f} - {row['product_name']}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(regenerate_embeddings())
