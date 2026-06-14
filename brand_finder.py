#!/usr/bin/env python3
"""
Brand Finder: Given a list of brand names, finds their official website,
generates an Amazon search link, and looks up contact emails via Prospeo.io.
"""

import time
import json
import re
import random
import sys
import argparse
import csv
import os
from dataclasses import dataclass, asdict, field
from typing import Optional, List
import warnings
warnings.filterwarnings("ignore")

import requests

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

# Prospeo API key (set via env variable or default below)
PROSPEO_API_KEY = os.environ.get(
    "PROSPEO_API_KEY",
    "pk_bf9cf188bea22288c22a10c7edaf1081da0d95208706531dac27ef0ca9c9ceb1"
)
PROSPEO_API_URL = "https://api.prospeo.io"


@dataclass
class BrandResult:
    input_brand: str
    amazon_search_url: str = ""
    official_website: Optional[str] = None
    emails: List[str] = field(default_factory=list)
    primary_email: Optional[str] = None
    confidence: str = "low"
    notes: str = ""


def random_delay(min_s=2.0, max_s=5.0):
    time.sleep(random.uniform(min_s, max_s))


def make_amazon_url(brand_name: str) -> str:
    query = brand_name.strip().replace(" ", "+")
    return f"https://www.amazon.com/s?k={query}"


def extract_domain(url: str) -> str:
    """Extract clean domain from a URL."""
    domain = re.sub(r'https?://(www\.)?', '', url)
    domain = domain.split('/')[0].split('?')[0]
    return domain.lower()


def find_official_website(brand_name: str) -> str:
    """Use DuckDuckGo to find the official website for a brand."""
    skip_domains = [
        "amazon.", "wikipedia.", "facebook.", "instagram.",
        "twitter.", "linkedin.", "yelp.", "reddit.", "youtube.",
        "tiktok.", "pinterest.", "walmart.", "ebay."
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

        for r in results:
            url = r.get("href", "")
            if not url or any(d in url for d in skip_domains):
                continue
            domain = re.sub(r'https?://(www\.)?', '', url).split('/')[0]
            domain_clean = re.sub(r'[^a-z0-9]', '', domain.lower())
            if brand_slug in domain_clean:
                return url

        for r in results:
            url = r.get("href", "")
            if url and not any(d in url for d in skip_domains):
                return url

        random_delay(2, 3)

    return ""


def prospeo_domain_search(domain: str) -> dict:
    """
    Call Prospeo domain-search API to find emails for a domain.
    Returns dict with 'emails' list and 'total' count.
    """
    if not domain:
        return {}

    headers = {
        "Content-Type": "application/json",
        "X-KEY": PROSPEO_API_KEY,
    }
    payload = {
        "domain": domain,
        "limit": 10,
    }

    try:
        resp = requests.post(
            f"{PROSPEO_API_URL}/domain-search",
            headers=headers,
            json=payload,
            timeout=15
        )
        data = resp.json()

        if resp.status_code != 200 or data.get("error"):
            print(f"  [prospeo] Error: {data.get('message', resp.status_code)}")
            return {}

        email_list = data.get("response", {}).get("emails", [])
        emails = [e.get("email") for e in email_list if e.get("email")]
        return {
            "emails": emails,
            "total": data.get("response", {}).get("total", 0),
        }

    except Exception as e:
        print(f"  [prospeo] Request failed: {e}")
        return {}


def prospeo_email_finder(first_name: str, last_name: str, domain: str) -> str:
    """
    Call Prospeo email-finder API to find a specific person's email.
    """
    if not domain:
        return ""

    headers = {
        "Content-Type": "application/json",
        "X-KEY": PROSPEO_API_KEY,
    }
    payload = {
        "first_name": first_name,
        "last_name": last_name,
        "company": domain,
    }

    try:
        resp = requests.post(
            f"{PROSPEO_API_URL}/email-finder",
            headers=headers,
            json=payload,
            timeout=15
        )
        data = resp.json()
        return data.get("response", {}).get("email", "")
    except Exception as e:
        print(f"  [prospeo] Email finder failed: {e}")
        return ""


def validate_with_claude(brand_name: str, website_candidate: str) -> dict:
    if not HAS_CLAUDE:
        return {}
    client = anthropic.Anthropic()
    prompt = f"""A scraper found this website candidate for the brand "{brand_name}":
Website: {website_candidate}

Is this the correct official website for the brand?
Reply with JSON only:
{{"is_correct": true, "official_website": "correct URL or empty string", "confidence": "high|medium|low", "notes": "brief note"}}"""

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


def save_results(results: list, output_file: str):
    """Save results to JSON and CSV."""
    with open(output_file, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)

    csv_file = output_file.replace(".json", ".csv")
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "input_brand", "amazon_search_url", "official_website",
            "primary_email", "emails", "confidence", "notes"
        ])
        writer.writeheader()
        for r in results:
            row = asdict(r)
            row["emails"] = ", ".join(r.emails) if r.emails else ""
            writer.writerow(row)


def process_brands(brands: list, use_claude: bool = True,
                   use_prospeo: bool = True, output_file: str = "results.json") -> list:
    results = []

    for i, brand in enumerate(brands, 1):
        brand = brand.strip()
        if not brand:
            continue

        print(f"\n[{i}/{len(brands)}] {brand}")
        result = BrandResult(input_brand=brand)

        # Step 1: Amazon URL (instant)
        result.amazon_search_url = make_amazon_url(brand)
        print(f"  Amazon search : {result.amazon_search_url}")

        # Step 2: Find official website
        print(f"  Finding official website...")
        website = find_official_website(brand)
        result.official_website = website

        if website:
            print(f"  Website found : {website}")
            result.confidence = "medium"
        else:
            print(f"  Website       : not found")

        # Step 3: Claude validation
        if use_claude and HAS_CLAUDE and website:
            print(f"  Validating with Claude...")
            validation = validate_with_claude(brand, website)
            if validation:
                if not validation.get("is_correct", True):
                    result.official_website = validation.get("official_website", website)
                    website = result.official_website
                result.confidence = validation.get("confidence", result.confidence)
                result.notes = validation.get("notes", "")
                print(f"  Confidence    : {result.confidence}")

        # Step 4: Prospeo email lookup
        if use_prospeo and website:
            domain = extract_domain(website)
            print(f"  Prospeo search: {domain}")
            prospeo_data = prospeo_domain_search(domain)
            if prospeo_data.get("emails"):
                result.emails = prospeo_data["emails"]
                result.primary_email = prospeo_data["emails"][0]
                print(f"  Emails found  : {', '.join(result.emails[:3])}")
                if len(result.emails) > 3:
                    print(f"                  ...and {len(result.emails) - 3} more")
            else:
                print(f"  Emails        : none found")

        results.append(result)
        save_results(results, output_file)

        if i < len(brands):
            random_delay(3, 6)

    return results


def print_summary(results: list):
    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)
    for r in results:
        print(f"\nBrand           : {r.input_brand}")
        print(f"  Amazon search : {r.amazon_search_url}")
        print(f"  Official site : {r.official_website or 'not found'}")
        print(f"  Primary email : {r.primary_email or 'not found'}")
        if r.emails and len(r.emails) > 1:
            print(f"  All emails    : {', '.join(r.emails)}")
        print(f"  Confidence    : {r.confidence}")
        if r.notes:
            print(f"  Notes         : {r.notes}")


def main():
    parser = argparse.ArgumentParser(
        description="Find official websites and contact emails for brands on Amazon."
    )
    parser.add_argument("brands", nargs="*", help="Brand names to look up.")
    parser.add_argument("--file", "-f", help="Text file with one brand per line.")
    parser.add_argument("--output", "-o", default="results.json", help="Output JSON file.")
    parser.add_argument("--no-claude", action="store_true", help="Disable Claude AI validation.")
    parser.add_argument("--no-prospeo", action="store_true", help="Disable Prospeo email lookup.")
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
    print(f"Prospeo email lookup: {'enabled' if not args.no_prospeo else 'disabled'}")

    results = process_brands(
        brands,
        use_claude=not args.no_claude,
        use_prospeo=not args.no_prospeo,
        output_file=args.output
    )
    print_summary(results)
    csv_file = args.output.replace(".json", ".csv")
    print(f"\nSaved to     : {args.output}")
    print(f"Spreadsheet  : {csv_file}  ← open in Excel or Numbers")


if __name__ == "__main__":
    main()
