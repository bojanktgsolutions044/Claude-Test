#!/usr/bin/env python3
"""
Brand Finder: Given a list of brand names, finds their official website
and generates a direct Amazon search link for each brand.
If no website is found, falls back to scraping Amazon for seller business info.
"""

import time
import json
import re
import random
import sys
import argparse
import csv
from dataclasses import dataclass, field, asdict
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
    import requests
    from bs4 import BeautifulSoup
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    print("WARNING: pip3 install requests beautifulsoup4  (needed for Amazon seller lookup)")

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
    seller_business_name: str = ""
    seller_business_address: str = ""


def random_delay(min_s=2.0, max_s=5.0):
    time.sleep(random.uniform(min_s, max_s))


def make_amazon_url(brand_name: str) -> str:
    query = brand_name.strip().replace(" ", "+")
    return f"https://www.amazon.com/s?k={query}"


def clean_url(url: str) -> str:
    """Strip tracking query parameters and return just the base URL."""
    return re.sub(r'\?.*$', '', url).rstrip('/')


def find_official_website(brand_name: str) -> str:
    skip_domains = [
        "amazon.", "wikipedia.", "facebook.", "instagram.",
        "twitter.", "linkedin.", "yelp.", "reddit.", "youtube.",
        "tiktok.", "pinterest.", "trustpilot.", "bbb.org",
        "glassdoor.", "indeed.", "zoominfo.", "dnb.com",
        "alternativeto.", "g2.com", "capterra.", "crunchbase.",
        "owler.", "craft.co", "comparably.", "sitejabber.",
        "sellersnooper.", "sellerapp.", "junglescout.", "helium10.",
        "keepa.", "camelcamelcamel.", "sellercentral.", "merchantwords.",
        "bing.com", "msn.com", "yahoo.com", "ask.com",
        "ebay.", "walmart.", "target.", "etsy.", "wayfair.",
        "homedepot.", "lowes.", "bestbuy.", "costco.",
    ]
    skip_url_patterns = [
        "promo-code", "promo_code", "coupon", "discount", "deals",
        "review", "/brand/", "store-list", "stores/", "tourdates",
        "software/", "/wiki/", "directory/", "seller-profiles/",
        "aclick", "/video/", "/news/", "/article/",
    ]

    queries = [
        f'"{brand_name}" official website',
        f"{brand_name} brand official site -wikipedia -amazon",
    ]

    brand_slug = re.sub(r'[^a-z0-9]', '', brand_name.lower())
    # Build word tokens for title matching (e.g. "Baby Sense" → ["baby", "sense"])
    brand_words = [w.lower() for w in brand_name.split() if len(w) > 2]

    def is_bad_url(url):
        if any(d in url for d in skip_domains):
            return True
        if any(p in url.lower() for p in skip_url_patterns):
            return True
        return False

    def title_matches(title: str) -> bool:
        """Check if the result title looks related to the brand."""
        t = title.lower()
        return any(w in t for w in brand_words)

    for query in queries:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=10))
        except Exception as e:
            print(f"  [search error] {e}")
            random_delay(3, 6)
            continue

        if not results:
            random_delay(2, 4)
            continue

        # First pass: domain contains the brand name (strongest signal)
        for r in results:
            url = r.get("href", "")
            if not url or is_bad_url(url):
                continue
            domain = re.sub(r'https?://(www\.)?', '', url).split('/')[0]
            domain_clean = re.sub(r'[^a-z0-9]', '', domain.lower())
            if brand_slug in domain_clean:
                return clean_url(url)

        # Second pass: domain contains at least one brand word
        for r in results:
            url = r.get("href", "")
            if not url or is_bad_url(url):
                continue
            domain = re.sub(r'https?://(www\.)?', '', url).split('/')[0]
            domain_clean = re.sub(r'[^a-z0-9]', '', domain.lower())
            if any(w in domain_clean for w in brand_words):
                return clean_url(url)

        random_delay(2, 3)

    return ""


def amazon_headers() -> dict:
    """Return headers that mimic a real browser to reduce Amazon blocking."""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }


def get_organic_product_urls(brand_name: str, max_results: int = 2) -> list:
    """
    Search Amazon for the brand and return URLs of the first organic
    (non-sponsored) product listings.
    """
    if not HAS_REQUESTS:
        return []

    search_url = make_amazon_url(brand_name)
    print(f"  Fetching Amazon search results...")

    try:
        resp = requests.get(search_url, headers=amazon_headers(), timeout=15)
        if resp.status_code != 200:
            print(f"  [amazon] Search page returned status {resp.status_code}")
            return []
    except Exception as e:
        print(f"  [amazon] Could not fetch search page: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # Each search result sits in a div with data-component-type="s-search-result"
    result_divs = soup.find_all("div", attrs={"data-component-type": "s-search-result"})

    organic_urls = []
    for div in result_divs:
        if len(organic_urls) >= max_results:
            break

        # Skip sponsored listings — they carry a "Sponsored" label
        sponsored = div.find(lambda tag: tag.name in ("span", "a") and
                             "Sponsored" in tag.get_text())
        if sponsored:
            continue

        # Find the product link (the main title link)
        link_tag = div.find("a", class_=re.compile(r"s-link-style|a-link-normal"))
        if not link_tag:
            link_tag = div.find("a", href=re.compile(r"/dp/"))
        if link_tag and link_tag.get("href"):
            href = link_tag["href"]
            if not href.startswith("http"):
                href = "https://www.amazon.com" + href
            organic_urls.append(href)

    return organic_urls


def get_seller_id_from_product(product_url: str) -> Optional[str]:
    """Visit a product page and return the seller ID from the 'Sold by' link."""
    if not HAS_REQUESTS:
        return None

    try:
        resp = requests.get(product_url, headers=amazon_headers(), timeout=15)
        if resp.status_code != 200:
            return None
    except Exception as e:
        print(f"  [amazon] Could not fetch product page: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # "Sold by" link typically points to /sp?seller=XXXX or contains seller= in href
    sold_by = soup.find("a", href=re.compile(r"seller="))
    if sold_by:
        href = sold_by["href"]
        match = re.search(r"seller=([A-Z0-9]+)", href)
        if match:
            return match.group(1)

    return None


def get_seller_info(seller_id: str) -> dict:
    """
    Visit the Amazon seller info page and extract business name and address
    from the 'Detailed Seller Information' section.
    """
    if not HAS_REQUESTS:
        return {}

    url = f"https://www.amazon.com/sp?seller={seller_id}"
    try:
        resp = requests.get(url, headers=amazon_headers(), timeout=15)
        if resp.status_code != 200:
            return {}
    except Exception as e:
        print(f"  [amazon] Could not fetch seller page: {e}")
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")

    info = {"business_name": "", "business_address": ""}

    # Find the "Detailed Seller Information" section
    detailed_section = soup.find(string=re.compile(r"Detailed Seller Information", re.I))
    if detailed_section:
        parent = detailed_section.find_parent()
        if parent:
            # Walk siblings/children to collect name and address text
            container = parent.find_parent()
            if container:
                text_blocks = [t.strip() for t in container.stripped_strings]
                # First non-header block is usually the business name
                capture = False
                collected = []
                for t in text_blocks:
                    if re.search(r"Detailed Seller Information", t, re.I):
                        capture = True
                        continue
                    if capture and t:
                        collected.append(t)
                if collected:
                    info["business_name"] = collected[0]
                    info["business_address"] = ", ".join(collected[1:5])

    return info


def find_seller_via_amazon(brand_name: str) -> dict:
    """
    Orchestrates the Amazon fallback:
    1. Search Amazon → find organic listings
    2. Visit product page → get seller ID
    3. Visit seller page → get business name + address
    """
    product_urls = get_organic_product_urls(brand_name)
    if not product_urls:
        print(f"  No organic Amazon listings found.")
        return {}

    for i, url in enumerate(product_urls, 1):
        print(f"  Checking product {i}: {url[:70]}...")
        random_delay(2, 4)
        seller_id = get_seller_id_from_product(url)
        if seller_id:
            print(f"  Seller ID found: {seller_id}")
            random_delay(2, 3)
            info = get_seller_info(seller_id)
            if info.get("business_name"):
                return info

    return {}


def validate_with_claude(brand_name: str, website_candidate: str) -> dict:
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

        result.amazon_search_url = make_amazon_url(brand)
        print(f"  Amazon search : {result.amazon_search_url}")

        print(f"  Finding official website...")
        website = find_official_website(brand)
        result.official_website = website

        if website:
            print(f"  Website found : {website}")
            result.confidence = "medium"

            if use_claude and HAS_CLAUDE:
                print(f"  Validating with Claude...")
                validation = validate_with_claude(brand, website)
                if validation:
                    if not validation.get("is_correct", True):
                        result.official_website = validation.get("official_website", website)
                    result.confidence = validation.get("confidence", result.confidence)
                    result.notes = validation.get("notes", "")
                    print(f"  Confidence    : {result.confidence}")
        else:
            print(f"  Website not found — trying Amazon seller lookup...")
            seller_info = find_seller_via_amazon(brand)
            if seller_info:
                result.seller_business_name = seller_info.get("business_name", "")
                result.seller_business_address = seller_info.get("business_address", "")
                print(f"  Business name : {result.seller_business_name}")
                print(f"  Address       : {result.seller_business_address}")
                result.confidence = "medium"
                result.notes = "Website not found; seller info retrieved from Amazon"
            else:
                print(f"  No seller info found either.")

        results.append(result)

        with open(output_file, "w") as f:
            json.dump([asdict(r) for r in results], f, indent=2)

        csv_file = output_file.replace(".json", ".csv")
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "input_brand", "amazon_search_url", "official_website",
                "confidence", "notes", "seller_business_name", "seller_business_address"
            ])
            writer.writeheader()
            writer.writerows([asdict(r) for r in results])

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
        if r.seller_business_name:
            print(f"  Business name : {r.seller_business_name}")
        if r.seller_business_address:
            print(f"  Address       : {r.seller_business_address}")
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
    csv_file = args.output.replace(".json", ".csv")
    print(f"\nSaved to: {args.output}")
    print(f"Spreadsheet: {csv_file}  ← open this in Excel or Numbers")


if __name__ == "__main__":
    main()
