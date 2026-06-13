#!/usr/bin/env python3
"""
Brand Finder: Given a list of brand names, finds their Amazon presence
and locates their official website.
"""

import time
import json
import re
import random
import sys
import argparse
from dataclasses import dataclass, asdict
from typing import Optional

import requests
from bs4 import BeautifulSoup

try:
    from duckduckgo_search import DDGS
    HAS_DDGS = True
except ImportError:
    HAS_DDGS = False
    print("[warn] duckduckgo_search not installed. Website lookup disabled.")
    print("       Run: pip install duckduckgo-search")

try:
    import anthropic
    HAS_CLAUDE = True
except ImportError:
    HAS_CLAUDE = False
    print("[warn] anthropic not installed. AI validation disabled.")
    print("       Run: pip install anthropic")


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass
class BrandResult:
    input_brand: str
    amazon_brand_name: Optional[str] = None
    amazon_seller: Optional[str] = None
    amazon_url: Optional[str] = None
    official_website: Optional[str] = None
    confidence: str = "low"
    notes: str = ""


def random_delay(min_s=2.0, max_s=5.0):
    time.sleep(random.uniform(min_s, max_s))


def search_amazon(brand_name: str) -> dict:
    """Search Amazon for a brand, return top result info."""
    query = brand_name.replace(" ", "+")
    url = f"https://www.amazon.com/s?k={query}&ref=nb_sb_noss"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        return {"error": str(e)}

    soup = BeautifulSoup(resp.text, "html.parser")

    # Check for CAPTCHA / block
    if "robot" in resp.text.lower() or "captcha" in resp.text.lower():
        return {"error": "Amazon returned a CAPTCHA or bot-detection page. Try again later or use a proxy."}

    results = []
    for item in soup.select('[data-component-type="s-search-result"]')[:5]:
        # Product title
        title_el = item.select_one("h2 a span")
        title = title_el.get_text(strip=True) if title_el else ""

        # Product URL
        link_el = item.select_one("h2 a")
        link = "https://www.amazon.com" + link_el["href"] if link_el and link_el.get("href") else ""

        # Brand / "Visit the X Store" link
        brand_el = item.select_one(".a-row .a-size-base+ .a-size-base, [data-action='a-popover'] a")
        brand = brand_el.get_text(strip=True) if brand_el else ""

        # Seller / "by X" text
        by_el = item.select_one(".a-row:has([href*='field-lbr_brands_browse-bin'])")
        seller = by_el.get_text(strip=True) if by_el else ""

        results.append({
            "title": title,
            "url": link,
            "brand": brand,
            "seller": seller,
        })

    if not results:
        return {"error": "No results found on Amazon."}

    return {"results": results, "search_url": url}


def get_amazon_brand_page(asin_or_url: str) -> dict:
    """Fetch a specific Amazon product page and extract brand/seller info."""
    try:
        resp = requests.get(asin_or_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        return {"error": str(e)}

    soup = BeautifulSoup(resp.text, "html.parser")

    brand = ""
    seller = ""

    # Brand from product details table
    for row in soup.select("#productDetails_techSpec_section_1 tr, #detailBullets_feature_div li"):
        text = row.get_text(strip=True)
        if "brand" in text.lower():
            brand = re.sub(r"(?i)brand\s*[:\-]?\s*", "", text).strip()
            break

    # "Brand: X" in sidebar
    brand_el = soup.select_one("#bylineInfo")
    if brand_el and not brand:
        brand = brand_el.get_text(strip=True)

    # Sold by
    seller_el = soup.select_one("#merchantInfoFeature_feature_div .offer-display-feature-text-message")
    if seller_el:
        seller = seller_el.get_text(strip=True)

    return {"brand": brand, "seller": seller}


def find_official_website(brand_name: str, company_name: str = "") -> str:
    """Use DuckDuckGo to find the official website for a brand."""
    if not HAS_DDGS:
        return ""

    query_terms = [
        f'"{brand_name}" official website',
        f'{company_name or brand_name} official site',
    ]

    for query in query_terms:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=5))
            for r in results:
                url = r.get("href", "")
                title = r.get("title", "").lower()
                body = r.get("body", "").lower()
                # Skip Amazon, Wikipedia, social media
                skip_domains = ["amazon.", "wikipedia.", "facebook.", "instagram.", "twitter.", "linkedin.", "yelp."]
                if any(d in url for d in skip_domains):
                    continue
                # Prefer results that mention "official" or match brand name
                brand_lower = brand_name.lower()
                if brand_lower in url.lower() or "official" in title or "official" in body:
                    return url
            # Return first non-Amazon result as fallback
            for r in results:
                url = r.get("href", "")
                if "amazon." not in url:
                    return url
        except Exception:
            continue
        random_delay(1, 2)

    return ""


def validate_with_claude(brand_name: str, candidates: dict) -> dict:
    """Use Claude to validate and pick the best brand/website match."""
    if not HAS_CLAUDE:
        return candidates

    client = anthropic.Anthropic()
    prompt = f"""You are helping identify the official website for a brand.

Brand searched: {brand_name}
Amazon search results: {json.dumps(candidates.get('amazon_results', []), indent=2)}
Website candidate: {candidates.get('website_candidate', '')}

Based on this data:
1. What is the most likely official company/brand name?
2. What is the most likely official website URL?
3. How confident are you? (high/medium/low)
4. Any notes?

Respond only with JSON in this format:
{{"brand_name": "...", "website": "...", "confidence": "high|medium|low", "notes": "..."}}"""

    try:
        resp = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )
        text = resp.content[0].text.strip()
        # Extract JSON from response
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"  [claude] Validation failed: {e}")

    return {}


def process_brands(brands: list[str], use_claude: bool = True, output_file: str = "results.json") -> list[BrandResult]:
    results = []

    for i, brand in enumerate(brands, 1):
        brand = brand.strip()
        if not brand:
            continue

        print(f"\n[{i}/{len(brands)}] Processing: {brand}")
        result = BrandResult(input_brand=brand)

        # Step 1: Search Amazon
        print(f"  -> Searching Amazon...")
        amazon_data = search_amazon(brand)

        if "error" in amazon_data:
            result.notes = amazon_data["error"]
            print(f"  [!] Amazon error: {amazon_data['error']}")
        else:
            top = amazon_data["results"][0] if amazon_data["results"] else {}
            result.amazon_brand_name = top.get("brand") or brand
            result.amazon_seller = top.get("seller", "")
            result.amazon_url = amazon_data["search_url"]

            print(f"  -> Found brand: {result.amazon_brand_name}")
            print(f"  -> Seller: {result.amazon_seller}")

        # Step 2: Find official website
        if HAS_DDGS:
            print(f"  -> Searching for official website...")
            company_name = result.amazon_brand_name or brand
            website = find_official_website(brand, company_name)
            result.official_website = website
            print(f"  -> Website candidate: {website or 'not found'}")

        # Step 3: Validate with Claude
        if use_claude and HAS_CLAUDE and amazon_data.get("results"):
            print(f"  -> Validating with Claude...")
            validation = validate_with_claude(brand, {
                "amazon_results": amazon_data.get("results", []),
                "website_candidate": result.official_website,
            })
            if validation:
                result.amazon_brand_name = validation.get("brand_name", result.amazon_brand_name)
                result.official_website = validation.get("website", result.official_website)
                result.confidence = validation.get("confidence", "low")
                result.notes = validation.get("notes", "")
                print(f"  -> Claude confidence: {result.confidence}")

        results.append(result)

        # Save progress after each brand
        with open(output_file, "w") as f:
            json.dump([asdict(r) for r in results], f, indent=2)

        # Polite delay between brands
        if i < len(brands):
            random_delay(3, 6)

    return results


def print_summary(results: list[BrandResult]):
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    for r in results:
        print(f"\nBrand: {r.input_brand}")
        print(f"  Amazon brand name : {r.amazon_brand_name or 'N/A'}")
        print(f"  Amazon seller     : {r.amazon_seller or 'N/A'}")
        print(f"  Official website  : {r.official_website or 'N/A'}")
        print(f"  Confidence        : {r.confidence}")
        if r.notes:
            print(f"  Notes             : {r.notes}")


def main():
    parser = argparse.ArgumentParser(
        description="Find official websites for brands selling on Amazon."
    )
    parser.add_argument(
        "brands",
        nargs="*",
        help="Brand names to look up (space-separated). Or use --file."
    )
    parser.add_argument(
        "--file", "-f",
        help="Path to a text file with one brand name per line."
    )
    parser.add_argument(
        "--output", "-o",
        default="results.json",
        help="Output JSON file (default: results.json)"
    )
    parser.add_argument(
        "--no-claude",
        action="store_true",
        help="Disable Claude AI validation step."
    )
    args = parser.parse_args()

    brands = list(args.brands)

    if args.file:
        with open(args.file) as f:
            brands += [line.strip() for line in f if line.strip()]

    if not brands:
        print("No brands provided. Example usage:")
        print("  python brand_finder.py 'Apple' 'Sony' 'Nike'")
        print("  python brand_finder.py --file brands.txt")
        sys.exit(1)

    print(f"Processing {len(brands)} brand(s)...")
    print(f"Output will be saved to: {args.output}")

    results = process_brands(brands, use_claude=not args.no_claude, output_file=args.output)
    print_summary(results)
    print(f"\nFull results saved to: {args.output}")


if __name__ == "__main__":
    main()
