#!/usr/bin/env python3
"""
Brand Finder: Given a list of brand names, finds their official website,
verifies company name via Prospeo, and extracts key contacts by job title.
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

PROSPEO_API_KEY = os.environ.get(
    "PROSPEO_API_KEY",
    "pk_bf9cf188bea22288c22a10c7edaf1081da0d95208706531dac27ef0ca9c9ceb1"
)
PROSPEO_API_URL = "https://api.prospeo.io"

# Job titles to extract from Prospeo results
TARGET_TITLES = [
    "founder", "owner", "co-founder", "cofounder",
    "president", "co-owner", "coowner",
    "vp of marketing", "vp marketing", "vice president of marketing",
    "marketing director", "director of marketing",
]


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
    # Key contacts matching target job titles
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
    all_matched_contacts: str = ""  # summary of all matches beyond 3
    confidence: str = "low"
    notes: str = ""


def random_delay(min_s=2.0, max_s=5.0):
    time.sleep(random.uniform(min_s, max_s))


def make_amazon_url(brand_name: str) -> str:
    query = brand_name.strip().replace(" ", "+")
    return f"https://www.amazon.com/s?k={query}"


def extract_domain(url: str) -> str:
    domain = re.sub(r'https?://(www\.)?', '', url)
    return domain.split('/')[0].split('?')[0].lower()


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


def is_target_title(title: str) -> bool:
    """Check if a job title matches one of our target titles."""
    title_lower = title.lower().strip()
    return any(t in title_lower for t in TARGET_TITLES)


def _prospeo_headers() -> dict:
    return {"Content-Type": "application/json", "X-KEY": PROSPEO_API_KEY}


def _prospeo_post(path: str, body: dict) -> Optional[dict]:
    """POST to a Prospeo endpoint; return parsed JSON or None on failure."""
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
    """
    Verify the real company name from a website domain via /search-company.
    Falls back to the company name on a matched person's CURRENT job.
    """
    # Try the documented nested filter shapes for search-company.
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
            break  # endpoint worked but no match; don't retry other shapes

    # Fallback: take the company name from a matched person's current job.
    for person in fallback_results:
        p = person.get("person", person)
        for job in p.get("job_history", []):
            if job.get("current") and job.get("company_name"):
                return job["company_name"]
    return ""


def prospeo_enrich_email(person: dict) -> str:
    """Get a verified email for a person via /enrich-person (uses LinkedIn URL)."""
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
        # Email can come back as a string or as a nested object.
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
    """
    NEW Prospeo API flow (old /domain-search was removed March 2026):
      1. /search-person  -> people at this domain filtered by target titles
      2. /search-company -> verified company name (with person fallback)
      3. /enrich-person  -> verified email for each matched person
    """
    if not domain:
        return {}

    titles_proper = [
        "Founder", "Owner", "Co-Founder", "President",
        "Co-Owner", "VP of Marketing", "Marketing Director",
    ]

    # Step 1: find people at this company with the target job titles.
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

    # Step 2: verified company name.
    company_name = prospeo_find_company_name(domain, people)

    # Step 3: filter by target title + enrich for email.
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
    with open(output_file, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)

    csv_file = output_file.replace(".json", ".csv")
    fieldnames = [
        "input_brand",
        "amazon_search_url",
        "official_website",
        "verified_company_name",
        "contact_1_name", "contact_1_title", "contact_1_email", "contact_1_linkedin",
        "contact_2_name", "contact_2_title", "contact_2_email", "contact_2_linkedin",
        "contact_3_name", "contact_3_title", "contact_3_email", "contact_3_linkedin",
        "all_matched_contacts",
        "confidence",
        "notes",
    ]
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows([asdict(r) for r in results])


def process_brands(brands: list, use_claude: bool = True,
                   use_prospeo: bool = True, output_file: str = "results.json") -> list:
    results = []

    for i, brand in enumerate(brands, 1):
        brand = brand.strip()
        if not brand:
            continue

        print(f"\n[{i}/{len(brands)}] {brand}")
        result = BrandResult(input_brand=brand)

        # Step 1: Amazon URL
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

                    # Fill up to 3 contact slots
                    for idx, slot in enumerate(["contact_1", "contact_2", "contact_3"]):
                        if idx < len(contacts):
                            c = contacts[idx]
                            setattr(result, f"{slot}_name", c.name)
                            setattr(result, f"{slot}_title", c.title)
                            setattr(result, f"{slot}_email", c.email)
                            setattr(result, f"{slot}_linkedin", c.linkedin)

                    # Summarise any extras beyond 3
                    if len(contacts) > 3:
                        extras = [f"{c.name} ({c.title})" for c in contacts[3:]]
                        result.all_matched_contacts = "; ".join(extras)
                else:
                    print(f"  Key contacts  : none with target titles")
            else:
                print(f"  Prospeo       : no data returned")

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
        description="Find brands on Amazon, verify company via Prospeo, extract key contacts."
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
