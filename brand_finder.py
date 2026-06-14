#!/usr/bin/env python3
"""
Brand Finder: Given a list of brand names, finds their official website,
verifies company name via Prospeo, and extracts key contacts by job title.
Also enriches with Amazon seller info, USPTO trademark owner, and LinkedIn URLs.
"""

import time
import json
import re
import random
import sys
import argparse
import csv
import os
from dataclasses import dataclass, asdict
from typing import Optional
import warnings
warnings.filterwarnings("ignore")

import requests
from bs4 import BeautifulSoup

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

PROSPEO_API_KEY = os.environ.get(
    "PROSPEO_API_KEY",
    "pk_bf9cf188bea22288c22a10c7edaf1081da0d95208706531dac27ef0ca9c9ceb1"
)
PROSPEO_API_URL = "https://api.prospeo.io"

TARGET_TITLES = [
    "founder", "owner", "co-founder", "cofounder",
    "president", "co-owner", "coowner",
    "vp of marketing", "vp marketing", "vice president of marketing",
    "marketing director", "director of marketing",
]

# Realistic browser headers so Amazon/USPTO don't immediately block us
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass
class Contact:
    name: str = ""
    title: str = ""
    email: str = ""
    linkedin: str = ""


@dataclass
class BrandResult:
    input_brand: str
    amazon_search_url: str = ""
    official_website: Optional[str] = None
    verified_company_name: Optional[str] = None
    # Amazon seller info
    amazon_seller_name: str = ""
    amazon_seller_address: str = ""
    # Trademark / ownership
    trademark_owner_entity: str = ""
    owner_full_name: str = ""
    # LinkedIn
    company_linkedin_url: str = ""
    owner_linkedin_url: str = ""
    # Key contacts from Prospeo
    contact_1_name: str = ""
    contact_1_title: str = ""
    contact_1_email: str = ""
    contact_1_linkedin: str = ""
    contact_2_name: str = ""
    contact_2_title: str = ""
    contact_2_email: str = ""
    contact_2_linkedin: str = ""
    contact_3_name: str = ""
    contact_3_title: str = ""
    contact_3_email: str = ""
    contact_3_linkedin: str = ""
    all_matched_contacts: str = ""
    confidence: str = "low"
    notes: str = ""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def random_delay(min_s=2.0, max_s=5.0):
    time.sleep(random.uniform(min_s, max_s))


def make_amazon_url(brand_name: str) -> str:
    query = brand_name.strip().replace(" ", "+")
    return f"https://www.amazon.com/s?k={query}"


def extract_domain(url: str) -> str:
    domain = re.sub(r'https?://(www\.)?', '', url)
    return domain.split('/')[0].split('?')[0].lower()


def ddg_search(query: str, max_results: int = 8) -> list:
    """DuckDuckGo text search; returns list of result dicts."""
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        print(f"  [ddg] {e}")
        random_delay(3, 6)
        return []


def safe_get(url: str, timeout: int = 12) -> Optional[requests.Response]:
    """HTTP GET with browser headers; returns None on any error."""
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=timeout)
        if resp.status_code == 200:
            return resp
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Step 1 — Amazon URL (unchanged)
# ---------------------------------------------------------------------------

def find_official_website(brand_name: str) -> str:
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
        results = ddg_search(query)
        if not results:
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


# ---------------------------------------------------------------------------
# NEW Stage A — Amazon "Sold by" → seller name + address
# ---------------------------------------------------------------------------

def _extract_seller_from_product_page(html: str, brand_name: str) -> dict:
    """
    Parse an Amazon product page for 'Sold by' seller name and the seller URL.
    Returns {"name": ..., "seller_url": ...}
    """
    soup = BeautifulSoup(html, "lxml")
    name = ""
    seller_url = ""

    # Pattern 1: #sellerProfileTriggerId or #merchant-info
    for tag in soup.select("#sellerProfileTriggerId, #merchantInfoFeature_feature_div a"):
        text = tag.get_text(strip=True)
        href = tag.get("href", "")
        if text and "Amazon" not in text:
            name = text
            if href:
                seller_url = "https://www.amazon.com" + href if href.startswith("/") else href
            break

    # Pattern 2: plain text "Sold by <name>"
    if not name:
        m = re.search(r'Sold by[:\s]+([A-Za-z0-9 &.,\-\']+)', html)
        if m:
            candidate = m.group(1).strip().rstrip(".")
            if candidate and "Amazon" not in candidate and len(candidate) < 80:
                name = candidate

    return {"name": name, "seller_url": seller_url}


def _extract_address_from_seller_page(html: str) -> str:
    """Parse an Amazon seller storefront page for a business address."""
    soup = BeautifulSoup(html, "lxml")
    # Seller info is usually inside #seller-about-section or .a-section
    for section in soup.select("#seller-about-section, #page-section-detail-seller-info"):
        text = section.get_text(" ", strip=True)
        # Look for a line that has a postal/zip pattern
        m = re.search(
            r'([A-Za-z0-9 .,#\-]+,\s*[A-Za-z]{2,}\s*\d{4,}[A-Za-z0-9 ,]*)',
            text
        )
        if m:
            return m.group(1).strip()
        # Fallback: any text block that mentions country-like words
        if any(kw in text for kw in ["Street", "Ave", "Road", "City", "State", "Country"]):
            return text[:200]
    return ""


def amazon_find_seller(brand_name: str) -> dict:
    """
    1. DDG → find an Amazon product listing for this brand
    2. Fetch the product page → parse 'Sold by' seller name + seller page URL
    3. Fetch the seller storefront page → parse address
    Returns {"seller_name": ..., "seller_address": ...}
    """
    result = {"seller_name": "", "seller_address": ""}

    # Find a product listing via DDG (avoids Amazon's bot-detection on searches)
    queries = [
        f'site:amazon.com "{brand_name}" "sold by"',
        f'site:amazon.com/dp "{brand_name}"',
    ]
    product_url = ""
    for q in queries:
        hits = ddg_search(q, max_results=5)
        for h in hits:
            url = h.get("href", "")
            if "amazon.com" in url and ("/dp/" in url or "/product/" in url):
                product_url = url
                break
            # DDG snippet sometimes includes "Sold by <Name>" text already
            snippet = h.get("body", "")
            m = re.search(r'[Ss]old by[:\s]+([A-Za-z0-9 &.,\-\']{3,60})', snippet)
            if m:
                candidate = m.group(1).strip().rstrip(".")
                if candidate and "Amazon" not in candidate:
                    result["seller_name"] = candidate
        if product_url or result["seller_name"]:
            break

    if result["seller_name"] and not product_url:
        return result  # got name from snippet alone

    if not product_url:
        return result

    print(f"  [amazon] Fetching product page...")
    random_delay(2, 4)
    resp = safe_get(product_url)
    if not resp:
        print(f"  [amazon] Product page blocked or failed")
        return result

    parsed = _extract_seller_from_product_page(resp.text, brand_name)
    if parsed["name"]:
        result["seller_name"] = parsed["name"]
        print(f"  [amazon] Seller: {parsed['name']}")

    # Fetch the seller's own page for the address
    if parsed["seller_url"]:
        random_delay(2, 3)
        seller_resp = safe_get(parsed["seller_url"])
        if seller_resp:
            addr = _extract_address_from_seller_page(seller_resp.text)
            if addr:
                result["seller_address"] = addr
                print(f"  [amazon] Address: {addr[:60]}...")

    return result


# ---------------------------------------------------------------------------
# NEW Stage B — USPTO trademark owner (free public API, no auth needed)
# ---------------------------------------------------------------------------

def uspto_find_owner(brand_name: str) -> dict:
    """
    Query the USPTO EFTS free JSON API for trademarks matching the brand.
    Returns {"trademark_owner_entity": ..., "owner_full_name": ...}
    """
    result = {"trademark_owner_entity": "", "owner_full_name": ""}

    # USPTO Trademark Electronic Search System — free, no API key
    search_url = "https://efts.uspto.gov/LATEST/search-efts"
    params = {
        "q": f'"{brand_name}"',
        "df": "mark_identification",
        "f": "json",
        "hits.hits._source": "mark_identification,owner,serial_number,registration_number",
    }

    try:
        resp = requests.get(search_url, params=params, headers=BROWSER_HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"  [uspto] HTTP {resp.status_code}")
            return result
        data = resp.json()
    except Exception as e:
        print(f"  [uspto] {e}")
        return result

    hits = (data.get("hits") or {}).get("hits") or []
    if not hits:
        print(f"  [uspto] No trademark found")
        return result

    # Take the first (most recent / best-scored) hit
    source = hits[0].get("_source", {})
    # Owner field can be a string or a list of dicts
    owner_raw = source.get("owner") or ""
    if isinstance(owner_raw, list):
        owner_raw = owner_raw[0] if owner_raw else {}
    if isinstance(owner_raw, dict):
        entity = (
            owner_raw.get("owner_name") or
            owner_raw.get("party_name") or
            owner_raw.get("name") or ""
        )
    else:
        entity = str(owner_raw)

    entity = entity.strip()
    if entity:
        result["trademark_owner_entity"] = entity
        print(f"  [uspto] Owner entity: {entity}")

    # Ask Claude to extract a personal name if one is embedded in the entity string
    if HAS_CLAUDE and entity:
        owner_name = _claude_extract_person_from_entity(brand_name, entity)
        if owner_name:
            result["owner_full_name"] = owner_name
            print(f"  [uspto] Owner person : {owner_name}")

    return result


def _claude_extract_person_from_entity(brand_name: str, entity: str) -> str:
    """
    Ask Claude whether the trademark owner entity string contains a personal name.
    Returns the person's full name, or empty string if the entity is a company.
    """
    if not HAS_CLAUDE:
        return ""
    try:
        client = anthropic.Anthropic()
        prompt = (
            f'The trademark for "{brand_name}" is owned by: "{entity}"\n\n'
            f'If this is a person\'s full name (not a company/LLC/Inc/Ltd), '
            f'return ONLY the person\'s full name as plain text with no extra words. '
            f'If it is a company or unclear, return exactly: COMPANY'
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}]
        )
        text = resp.content[0].text.strip()
        if text and text != "COMPANY" and len(text) < 80:
            return text
    except Exception as e:
        print(f"  [claude] {e}")
    return ""


# ---------------------------------------------------------------------------
# NEW Stage C — LinkedIn URLs via DuckDuckGo
# ---------------------------------------------------------------------------

def find_linkedin_urls(brand_name: str, company_name: str, owner_name: str) -> dict:
    """
    Search for LinkedIn company page and (optionally) owner's profile via DDG.
    Returns {"company_linkedin_url": ..., "owner_linkedin_url": ...}
    """
    result = {"company_linkedin_url": "", "owner_linkedin_url": ""}
    search_name = company_name or brand_name

    # Company page
    hits = ddg_search(f'"{search_name}" site:linkedin.com/company', max_results=5)
    for h in hits:
        url = h.get("href", "")
        if "linkedin.com/company/" in url:
            result["company_linkedin_url"] = url.split("?")[0]
            print(f"  [linkedin] Company: {result['company_linkedin_url']}")
            break

    random_delay(1, 3)

    # Owner's personal profile (only search if we have a name)
    if owner_name:
        hits = ddg_search(
            f'"{owner_name}" site:linkedin.com/in', max_results=5
        )
        for h in hits:
            url = h.get("href", "")
            if "linkedin.com/in/" in url:
                result["owner_linkedin_url"] = url.split("?")[0]
                print(f"  [linkedin] Owner  : {result['owner_linkedin_url']}")
                break

    return result


# ---------------------------------------------------------------------------
# Prospeo helpers (unchanged)
# ---------------------------------------------------------------------------

def is_target_title(title: str) -> bool:
    title_lower = title.lower().strip()
    return any(t in title_lower for t in TARGET_TITLES)


def _prospeo_headers() -> dict:
    return {"Content-Type": "application/json", "X-KEY": PROSPEO_API_KEY}


def _prospeo_post(path: str, body: dict) -> Optional[dict]:
    try:
        resp = requests.post(
            f"{PROSPEO_API_URL}{path}",
            headers=_prospeo_headers(),
            json=body,
            timeout=20,
        )
        data = resp.json()
        if resp.status_code != 200 or data.get("error"):
            msg = data.get("error_toast") or data.get("message") or resp.status_code
            print(f"  [prospeo] {path} -> {msg}")
            return None
        return data
    except Exception as e:
        print(f"  [prospeo] {path} request failed: {e}")
        return None


def prospeo_find_company_name(domain: str, fallback_results: list) -> str:
    company_filter_variants = [
        {"page": 1, "filters": {"company": {"domains": {"include": [domain]}}}},
        {"page": 1, "filters": {"company_domain": {"include": [domain]}}},
        {"page": 1, "filters": {"company": {"websites": {"include": [domain]}}}},
    ]
    for body in company_filter_variants:
        data = _prospeo_post("/search-company", body)
        if data:
            results = data.get("results") or []
            if results:
                comp = results[0].get("company", results[0])
                name = comp.get("name") or comp.get("company_name") or ""
                if name:
                    return name
            break

    for person in fallback_results:
        p = person.get("person", person)
        for job in p.get("job_history", []):
            if job.get("current") and job.get("company_name"):
                return job["company_name"]
    return ""


def prospeo_enrich_email(person: dict) -> str:
    p = person.get("person", person)
    linkedin = p.get("linkedin_url") or ""
    person_id = p.get("person_id") or ""

    enrich_bodies = []
    if linkedin:
        enrich_bodies.append({"linkedin_url": linkedin})
    if person_id:
        enrich_bodies.append({"person_id": person_id})

    for body in enrich_bodies:
        data = _prospeo_post("/enrich-person", body)
        if not data:
            continue
        resp = data.get("response", data)
        email_field = resp.get("email")
        if isinstance(email_field, dict):
            email = email_field.get("email") or email_field.get("value") or ""
        else:
            email = email_field or ""
        if not email:
            emails = resp.get("emails") or []
            if emails and isinstance(emails[0], dict):
                email = emails[0].get("email", "")
            elif emails:
                email = emails[0]
        if email:
            return email
    return ""


def prospeo_domain_search(domain: str) -> dict:
    if not domain:
        return {}

    titles_proper = [
        "Founder", "Owner", "Co-Founder", "President",
        "Co-Owner", "VP of Marketing", "Marketing Director",
    ]

    person_variants = [
        {"page": 1, "filters": {
            "company": {"domains": {"include": [domain]}},
            "person_job_title": {"include": titles_proper},
        }},
        {"page": 1, "filters": {
            "company": {"domains": {"include": [domain]}},
        }},
    ]
    people = []
    for body in person_variants:
        data = _prospeo_post("/search-person", body)
        if data is not None:
            people = data.get("results") or []
            if people:
                break

    company_name = prospeo_find_company_name(domain, people)

    matched_contacts = []
    for person in people:
        p = person.get("person", person)
        title = p.get("current_job_title") or p.get("headline") or ""
        if not (title and is_target_title(title)):
            continue
        full_name = p.get("full_name") or (
            f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        )
        linkedin = p.get("linkedin_url") or ""
        email = prospeo_enrich_email(person)
        matched_contacts.append(Contact(
            name=full_name,
            title=title,
            email=email,
            linkedin=linkedin,
        ))

    return {
        "company_name": company_name,
        "matched_contacts": matched_contacts,
        "total": len(people),
    }


# ---------------------------------------------------------------------------
# Claude website validator (unchanged)
# ---------------------------------------------------------------------------

def validate_with_claude(brand_name: str, website_candidate: str) -> dict:
    if not HAS_CLAUDE:
        return {}
    client = anthropic.Anthropic()
    prompt = (
        f'A scraper found this website candidate for the brand "{brand_name}":\n'
        f'Website: {website_candidate}\n\n'
        f'Is this the correct official website for the brand?\n'
        f'Reply with JSON only:\n'
        f'{{"is_correct": true, "official_website": "correct URL or empty string", '
        f'"confidence": "high|medium|low", "notes": "brief note"}}'
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
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


# ---------------------------------------------------------------------------
# Save results — JSON + CSV
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "input_brand",
    "amazon_search_url",
    "official_website",
    "verified_company_name",
    "amazon_seller_name",
    "amazon_seller_address",
    "trademark_owner_entity",
    "owner_full_name",
    "company_linkedin_url",
    "owner_linkedin_url",
    "contact_1_name", "contact_1_title", "contact_1_email", "contact_1_linkedin",
    "contact_2_name", "contact_2_title", "contact_2_email", "contact_2_linkedin",
    "contact_3_name", "contact_3_title", "contact_3_email", "contact_3_linkedin",
    "all_matched_contacts",
    "confidence",
    "notes",
]


def save_results(results: list, output_file: str):
    with open(output_file, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)

    csv_file = output_file.replace(".json", ".csv")
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([asdict(r) for r in results])


# ---------------------------------------------------------------------------
# Main per-brand loop
# ---------------------------------------------------------------------------

def process_brands(brands: list, use_claude: bool = True,
                   use_prospeo: bool = True, output_file: str = "results.json") -> list:
    results = []

    for i, brand in enumerate(brands, 1):
        brand = brand.strip()
        if not brand:
            continue

        print(f"\n[{i}/{len(brands)}] {brand}")
        result = BrandResult(input_brand=brand)

        # Step 1: Amazon search URL
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

        # Step 4: Prospeo — verify company name + find key contacts
        if use_prospeo and website:
            domain = extract_domain(website)
            print(f"  Prospeo lookup: {domain}")
            prospeo = prospeo_domain_search(domain)
            if prospeo:
                result.verified_company_name = prospeo.get("company_name", "")
                if result.verified_company_name:
                    print(f"  Company name  : {result.verified_company_name}")
                contacts = prospeo.get("matched_contacts", [])
                if contacts:
                    print(f"  Key contacts  : {len(contacts)} found")
                    for c in contacts:
                        print(f"    - {c.name} | {c.title} | {c.email}")
                    for idx, slot in enumerate(["contact_1", "contact_2", "contact_3"]):
                        if idx < len(contacts):
                            c = contacts[idx]
                            setattr(result, f"{slot}_name", c.name)
                            setattr(result, f"{slot}_title", c.title)
                            setattr(result, f"{slot}_email", c.email)
                            setattr(result, f"{slot}_linkedin", c.linkedin)
                    if len(contacts) > 3:
                        extras = [f"{c.name} ({c.title})" for c in contacts[3:]]
                        result.all_matched_contacts = "; ".join(extras)
                else:
                    print(f"  Key contacts  : none with target titles")

        # Step 5: Amazon seller info (company name + address from listing)
        print(f"  Amazon seller lookup...")
        seller = amazon_find_seller(brand)
        result.amazon_seller_name = seller.get("seller_name", "")
        result.amazon_seller_address = seller.get("seller_address", "")

        # Step 6: USPTO trademark owner
        print(f"  USPTO lookup...")
        random_delay(1, 2)
        trademark = uspto_find_owner(brand)
        result.trademark_owner_entity = trademark.get("trademark_owner_entity", "")
        result.owner_full_name = trademark.get("owner_full_name", "")

        # Step 7: LinkedIn URLs
        print(f"  LinkedIn lookup...")
        company_for_linkedin = (
            result.verified_company_name or
            result.amazon_seller_name or
            brand
        )
        linkedin = find_linkedin_urls(brand, company_for_linkedin, result.owner_full_name)
        result.company_linkedin_url = linkedin.get("company_linkedin_url", "")
        result.owner_linkedin_url = linkedin.get("owner_linkedin_url", "")

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
        print(f"\nBrand            : {r.input_brand}")
        print(f"  Amazon search  : {r.amazon_search_url}")
        print(f"  Official site  : {r.official_website or 'not found'}")
        print(f"  Company name   : {r.verified_company_name or 'not verified'}")
        print(f"  Amazon seller  : {r.amazon_seller_name or '-'}")
        print(f"  Seller address : {r.amazon_seller_address or '-'}")
        print(f"  TM owner entity: {r.trademark_owner_entity or '-'}")
        print(f"  Owner name     : {r.owner_full_name or '-'}")
        print(f"  Company LI     : {r.company_linkedin_url or '-'}")
        print(f"  Owner LI       : {r.owner_linkedin_url or '-'}")
        for slot in ["contact_1", "contact_2", "contact_3"]:
            name = getattr(r, f"{slot}_name")
            if name:
                title = getattr(r, f"{slot}_title")
                email = getattr(r, f"{slot}_email")
                print(f"  Contact        : {name} | {title} | {email}")
        print(f"  Confidence     : {r.confidence}")
        if r.notes:
            print(f"  Notes          : {r.notes}")


def main():
    parser = argparse.ArgumentParser(
        description="Find brands on Amazon, verify company via Prospeo, extract contacts, owner, LinkedIn."
    )
    parser.add_argument("brands", nargs="*", help="Brand names to look up.")
    parser.add_argument("--file", "-f", help="Text file with one brand per line.")
    parser.add_argument("--output", "-o", default="results.json", help="Output JSON file.")
    parser.add_argument("--no-claude", action="store_true", help="Disable Claude AI validation.")
    parser.add_argument("--no-prospeo", action="store_true", help="Disable Prospeo lookup.")
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
