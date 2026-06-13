#!/usr/bin/env python3
"""
Brand Finder: Given a list of brand names, finds their Amazon presence
and locates their official website — using DuckDuckGo to avoid Amazon blocks.
"""

import time
import json
import re
import random
import sys
import argparse
from dataclasses import dataclass, asdict
from typing import Optional

try:
    from ddgs import DDGS
    HAS_DDGS = True
except ImportError:
    try:
        from duckduckgo_search import DDGS
        HAS_DDGS = True
    except ImportError:
        HAS_DDGS = False
        print("[warn] ddgs not installed. Run: pip install ddgs")

try:
    import anthropic
    HAS_CLAUDE = True
except ImportError:
    HAS_CLAUDE = False


@dataclass
class BrandResult:
    input_brand: str
    amazon_brand_name: Optional[str] = None
    amazon_seller: Optional[str] = None
    amazon_url: Optional[str] = None
    official_website: Optional[str] = None
    confidence: str = "low"
    notes: str = ""


def random_delay(min_s=2.0, max_s=4.0):
    time.sleep(random.uniform(min_s, max_s))


def ddg_search(query: str, max_results: int = 5) -> list:
    """Run a DuckDuckGo search and return results."""
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        print(f"  [ddg error] {e}")
        return []


def search_amazon_via_ddg(brand_name: str) -> dict:
    """
    Search for a brand on Amazon using DuckDuckGo (avoids Amazon bot detection).
    Returns brand name, seller, and Amazon URL extracted from search results.
    """
    query = f"{brand_name} amazon.com brand store"
    results = ddg_search(query, max_results=5)

    if not results:
        return {"error": "No results from DuckDuckGo."}

    amazon_results = []
    for r in results:
        url = r.get("href", "")
        title = r.get("title", "")
        body = r.get("body", "")

        if "amazon.com" not in url:
            continue

        # Try to extract brand/seller from snippet
        brand_match = re.search(r'(?:by|Brand[:\s]+|Visit the\s+)([A-Z][^\s,\.]+(?:\s+[A-Z][^\s,\.]+)?)\s+(?:Store|brand)?', title + " " + body)
        extracted_brand = brand_match.group(1) if brand_match else brand_name

        amazon_results.append({
            "title": title,
            "url": url,
            "brand": extracted_brand,
            "snippet": body[:200],
        })

    if not amazon_results:
        # Fall back: just return the first result URL even if not amazon.com
        first = results[0]
        return {
            "results": [{
                "title": first.get("title", ""),
                "url": first.get("href", ""),
                "brand": brand_name,
                "snippet": first.get("body", "")[:200],
            }],
            "note": "No direct Amazon URL found; showing best match."
        }

    return {"results": amazon_results}


def find_official_website(brand_name: str, company_name: str = "") -> str:
    """Use DuckDuckGo to find the official website for a brand."""
    if not HAS_DDGS:
        return ""

    search_name = company_name if company_name and company_name != brand_name else brand_name
    queries = [
        f'"{brand_name}" official website',
        f'{search_name} official site -amazon -wikipedia',
    ]

    skip_domains = ["amazon.", "wikipedia.", "facebook.", "instagram.",
                    "twitter.", "linkedin.", "yelp.", "reddit.", "youtube."]

    for query in queries:
        results = ddg_search(query, max_results=6)
        for r in results:
            url = r.get("href", "")
            title = r.get("title", "").lower()
            body = r.get("body", "").lower()
            if any(d in url for d in skip_domains):
                continue
            brand_lower = brand_name.lower().replace(" ", "")
            domain = re.sub(r'https?://(www\.)?', '', url).split('/')[0].replace("-", "").replace(".", "")
            if brand_lower in domain or "official" in title or "official" in body:
                return url
        # Fallback: first non-skip result
        for r in results:
            url = r.get("href", "")
            if not any(d in url for d in skip_domains):
                return url
        random_delay(1, 2)

    return ""


def validate_with_claude(brand_name: str, candidates: dict) -> dict:
    """Use Claude to validate and pick the best brand/website match."""
    if not HAS_CLAUDE:
        return {}

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

Respond only with JSON:
{{"brand_name": "...", "website": "...", "confidence": "high|medium|low", "notes": "..."}}"""

    try:
        resp = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )
        text = resp.content[0].text.strip()
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"  [claude] Validation failed: {e}")

    return {}


def process_brands(brands: list, use_claude: bool = True, output_file: str = "results.json") -> list:
    results = []

    for i, brand in enumerate(brands, 1):
        brand = brand.strip()
        if not brand:
            continue

        print(f"\n[{i}/{len(brands)}] Processing: {brand}")
        result = BrandResult(input_brand=brand)

        # Step 1: Find Amazon listing via DuckDuckGo
        print(f"  -> Searching Amazon (via DuckDuckGo)...")
        amazon_data = search_amazon_via_ddg(brand)

        if "error" in amazon_data:
            result.notes = amazon_data["error"]
            print(f"  [!] {amazon_data['error']}")
        else:
            top = amazon_data["results"][0] if amazon_data.get("results") else {}
            result.amazon_brand_name = top.get("brand") or brand
            result.amazon_url = top.get("url", "")
            if amazon_data.get("note"):
                result.notes = amazon_data["note"]
            print(f"  -> Amazon brand : {result.amazon_brand_name}")
            print(f"  -> Amazon URL   : {result.amazon_url}")

        random_delay(1, 2)

        # Step 2: Find official website
        print(f"  -> Searching for official website...")
        website = find_official_website(brand, result.amazon_brand_name or "")
        result.official_website = website
        print(f"  -> Website      : {website or 'not found'}")

        # Step 3: Validate with Claude
        if use_claude and HAS_CLAUDE and amazon_data.get("results"):
            print(f"  -> Validating with Claude AI...")
            validation = validate_with_claude(brand, {
                "amazon_results": amazon_data.get("results", []),
                "website_candidate": result.official_website,
            })
            if validation:
                result.amazon_brand_name = validation.get("brand_name", result.amazon_brand_name)
                result.official_website = validation.get("website", result.official_website)
                result.confidence = validation.get("confidence", "low")
                result.notes = validation.get("notes", result.notes)
                print(f"  -> Confidence   : {result.confidence}")
        elif not use_claude or not HAS_CLAUDE:
            result.confidence = "medium" if result.official_website else "low"

        results.append(result)

        # Save progress after each brand
        with open(output_file, "w") as f:
            json.dump([asdict(r) for r in results], f, indent=2)

        if i < len(brands):
            random_delay(3, 5)

    return results


def print_summary(results: list):
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    for r in results:
        print(f"\nBrand           : {r.input_brand}")
        print(f"  Amazon name   : {r.amazon_brand_name or 'N/A'}")
        print(f"  Amazon URL    : {r.amazon_url or 'N/A'}")
        print(f"  Official site : {r.official_website or 'N/A'}")
        print(f"  Confidence    : {r.confidence}")
        if r.notes:
            print(f"  Notes         : {r.notes}")


def main():
    parser = argparse.ArgumentParser(
        description="Find official websites for brands selling on Amazon."
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
        print("No brands provided. Example:")
        print("  python3 brand_finder.py 'Nike' 'Sony'")
        print("  python3 brand_finder.py --file brands.txt")
        sys.exit(1)

    print(f"Processing {len(brands)} brand(s)... Results saved to: {args.output}")
    results = process_brands(brands, use_claude=not args.no_claude, output_file=args.output)
    print_summary(results)
    print(f"\nFull results saved to: {args.output}")


if __name__ == "__main__":
    main()
