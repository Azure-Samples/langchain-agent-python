#!/usr/bin/env python3
"""
Generate products_pregenerated.json from product_data.json

This script extracts products from the nested product_data.json structure
and creates a flat, pre-processed JSON file for fast database loading.
"""

import json
from pathlib import Path


def generate_products_json():
    """Extract products from product_data.json and save to products_pregenerated.json."""
    
    data_dir = Path(__file__).parent
    product_data_file = data_dir / 'product_data.json'
    output_file = data_dir / 'products_pregenerated.json'
    
    print("=" * 60)
    print("Generate Pre-Generated Products JSON")
    print("=" * 60)
    
    # Load product data
    print(f"\nLoading {product_data_file}...")
    with open(product_data_file) as f:
        product_data = json.load(f)
    
    main_categories = product_data.get('main_categories', {})
    
    # Extract all products into flat list
    products = []
    product_count = 0
    
    for category_name, category_data in main_categories.items():
        for product_type_name, product_list in category_data.items():
            # Skip seasonal multipliers and non-list values
            if product_type_name == 'washington_seasonal_multipliers':
                continue
            if not isinstance(product_list, list):
                continue
            
            for product in product_list:
                if not isinstance(product, dict):
                    continue
                
                product_count += 1
                
                # Calculate selling price from cost for 33% gross margin
                cost = float(product.get('price', 0))  # JSON price is the cost
                base_price = round(cost / 0.67, 2)  # Selling price = Cost / (1 - 0.33)
                
                # Create flattened product record
                product_record = {
                    'sku': product.get('sku', f"SKU-{product_count:06d}"),
                    'product_name': product.get('name'),
                    'product_description': product.get('description'),
                    'category_name': category_name,
                    'type_name': product_type_name,
                    'cost': cost,
                    'base_price': base_price,
                    'gross_margin_percent': 33.0
                }
                
                # Add embeddings if available
                if 'image_embedding' in product and product['image_embedding']:
                    embedding = product['image_embedding']
                    if isinstance(embedding, list) and len(embedding) == 512:
                        product_record['image_embedding'] = embedding
                        product_record['image_path'] = product.get('image_path', '')
                
                if 'description_embedding' in product and product['description_embedding']:
                    embedding = product['description_embedding']
                    if isinstance(embedding, list) and len(embedding) == 1536:
                        product_record['description_embedding'] = embedding
                
                products.append(product_record)
                
                if product_count % 100 == 0:
                    print(f"  Processed {product_count} products...")
    
    # Save to JSON file
    print(f"\nWriting {len(products)} products to {output_file}...")
    with open(output_file, 'w') as f:
        json.dump(products, f, indent=2)
    
    # Report file size
    file_size_mb = output_file.stat().st_size / (1024 * 1024)
    
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Products extracted: {len(products)}")
    print(f"Output file: {output_file.name}")
    print(f"File size: {file_size_mb:.2f} MB")
    
    if file_size_mb > 100:
        print("❌ Error: File size exceeds GitHub's 100 MB limit")
    elif file_size_mb > 50:
        print("⚠️  Warning: File size exceeds GitHub's 50 MB recommendation")
    else:
        print("✅ File size is safe for GitHub")
    
    print("=" * 60)


if __name__ == "__main__":
    generate_products_json()
