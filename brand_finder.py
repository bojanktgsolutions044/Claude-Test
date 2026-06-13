#!/usr/bin/env python3
"""
Brand Finder: Given a list of brand names, finds their official website
and generates a direct Amazon search link for each brand.
"""

import time
import json
import re
import random
import sys
import argparse
from dataclasses import dataclass, asdict
from typing import Optional
import warnings
warnings.filterwarnings("ignore")

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        print("ERROR: Run this first:  pip3 install ddgs")
        sys.exit(1)

try:
    import anthropic
    HAS_CLAUDE = True
except ImportError:
    HAS_CLAUDE = False


@dataclass
class BrandResult:
    input_brand: str
    amazon_search_url: str = ""
    official_website: Optional[str] = None
    confidence: str = "low"
    notes: str = ""


def random_delay(min_s=2.0, max_s=5.0):
    time.sleep(random.uniform(min_s, max_s))


def make_amazon_url(brand_name: str) -> str:
    """Build a direct Amazon search URL for the brand."""
    query = brand_name.strip().replace(" ", "+")
    return f"https://www.amazon.com/s?k={query}"


def find_official_website(brand_name: str) -> str:
    """Use DuckDuckGo to find the official website for a brand."""
    skip_domains = [
        "amazon.", "wikipedia.", "facebook.", "instagram.",
        "twitter.", "linkedin.", "yelp.", "reddit.", "youtube.",
        "tiktok.", "pinterest."
    ]

    queries = [
        f"{brand_name} official website",
        f"{brand_name} homepage -wikipedia -amazon",
    ]

    for query in queries:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=8))
        except Exception as e:
            print(f"  [search error] {e}")
            random_delay(3, 6)
            continue

        if not results:
            random_delay(2, 4)
            continue

        brand_slug = re.sub(r'[^a-z0-9]', '', brand_name.lower())

        # First pass: prefer URLs that contain the brand name
        for r in results:
            url = r.get("href", "")
            if not url or any(d in url for d in skip_domains):
                continue
            domain = re.sub(r'https?://(www\.)?', '', url).split('/')[0]
            domain_clean = re.sub(r'[^a-z0-9]', '', domain.lower())
            if brand_slug in domain_clean:
                return url

        # Second pass: first non-skipped result
        for r in results:
            url = r.get("href", "")
            if url and not any(d in url for d in skip_domains):
                return url

        random_delay(2, 3)

    return ""


def validate_with_claude(brand_name: str, website_candidate: str) -> dict:
    """Use Claude to validate the website match."""
    if not HAS_CLAUDE:
        return {}
    client = anthropic.Anthropic()
    prompt = f"""A scraper found this website candidate for the brand "{brand_name}":
Website: {website_candidate}

Is this the correct official website for the brand?
Reply with JSON only:
{{"is_correct": true/false, "official_website": "correct URL or empty string", "confidence": "high|medium|low", "notes": "brief note"}}"""

    try:
        resp = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        text = resp.content[0].text.strip()
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"  [claude] {e}")
    return {}


def process_brands(brands: list, use_claude: bool = True, output_file: str = "results.json") -> list:
    results = []

    for i, brand in enumerate(brands, 1):
        brand = brand.strip()
        if not brand:
            continue

        print(f"\n[{i}/{len(brands)}] {brand}")
        result = BrandResult(input_brand=brand)

        # Always generate Amazon search URL (no scraping needed)
        result.amazon_search_url = make_amazon_url(brand)
        print(f"  Amazon search : {result.amazon_search_url}")

        # Find official website via DuckDuckGo
        print(f"  Finding official website...")
        website = find_official_website(brand)
        result.official_website = website

        if website:
            print(f"  Website found : {website}")
            result.confidence = "medium"
        else:
            print(f"  Website       : not found")

        # Optional Claude validation
        if use_claude and HAS_CLAUDE and website:
            print(f"  Validating with Claude...")
            validation = validate_with_claude(brand, website)
            if validation:
                if not validation.get("is_correct", True):
                    result.official_website = validation.get("official_website", website)
                result.confidence = validation.get("confidence", result.confidence)
                result.notes = validation.get("notes", "")
                print(f"  Confidence    : {result.confidence}")

        results.append(result)

        # Save progress after each brand
        with open(output_file, "w") as f:
            json.dump([asdict(r) for r in results], f, indent=2)

        if i < len(brands):
            random_delay(3, 6)

    return results


def print_summary(results: list):
    print("\n" + "=" * 65)
    print("RESULTS SUMMARY")
    print("=" * 65)
    for r in results:
        print(f"\nBrand           : {r.input_brand}")
        print(f"  Amazon search : {r.amazon_search_url}")
        print(f"  Official site : {r.official_website or 'not found'}")
        print(f"  Confidence    : {r.confidence}")
        if r.notes:
            print(f"  Notes         : {r.notes}")


def main():
    parser = argparse.ArgumentParser(
        description="Find official websites for brands and generate Amazon search links."
    )
    parser.add_argument("brands", nargs="*", help="Brand names to look up.")
    parser.add_argument("--file", "-f", help="Text file with one brand per line.")
    parser.add_argument("--output", "-o", default="results.json", help="Output JSON file.")
    parser.add_argument("--no-claude", action="store_true", help="Disable Claude AI validation.")
    args = parser.parse_args()

    brands = list(args.brands)
    if args.file:
        with open(args.file) as f:
            brands += [line.strip() for line in f if line.strip()]

    if not brands:
        print("No brands provided. Examples:")
        print("  python3 brand_finder.py 'Nike' 'Sony'")
        print("  python3 brand_finder.py --file brands.txt")
        sys.exit(1)

    print(f"Processing {len(brands)} brand(s)...")
    results = process_brands(brands, use_claude=not args.no_claude, output_file=args.output)
    print_summary(results)
    print(f"\nSaved to: {args.output}")


if __name__ == "__main__":
    main()
