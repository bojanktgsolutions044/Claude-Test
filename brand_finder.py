#!/usr/bin/env python3
"""
Brand Finder — full enrichment pipeline:
  1. Amazon: find organic listing → seller page → Business Name + Address
  2. Official website: homepage only, verified via Claude
  3. Contact: email or contact-form URL from the official site
  4. USPTO: search by Business Name → owner first/last name
  5. Prospeo: owner name → LinkedIn URL + email
"""

import time, json, re, random, sys, argparse, csv, os
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
        print("ERROR: pip3 install ddgs"); sys.exit(1)

try:
    import anthropic
    HAS_CLAUDE = True
except ImportError:
    HAS_CLAUDE = False

# ── API keys ────────────────────────────────────────────────────────────────
PROSPEO_API_KEY = os.environ.get(
    "PROSPEO_API_KEY",
    "pk_bf9cf188bea22288c22a10c7edaf1081da0d95208706531dac27ef0ca9c9ceb1"
)
PROSPEO_URL = "https://api.prospeo.io"

NO_INFO = "No Info"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── Data model ───────────────────────────────────────────────────────────────
@dataclass
class BrandResult:
    input_brand: str
    # Amazon
    amazon_search_url: str = NO_INFO
    amazon_product_url: str = NO_INFO
    amazon_seller_name: str = NO_INFO
    amazon_seller_address: str = NO_INFO
    # Official website
    official_website: str = NO_INFO
    # Contact
    contact_email: str = NO_INFO
    contact_form_url: str = NO_INFO
    # USPTO
    trademark_owner_first_name: str = NO_INFO
    trademark_owner_last_name: str = NO_INFO
    trademark_owner_email: str = NO_INFO
    # Prospeo / LinkedIn
    owner_linkedin_url: str = NO_INFO
    owner_email_prospeo: str = NO_INFO
    # Meta
    notes: str = ""


# ── Helpers ──────────────────────────────────────────────────────────────────
def delay(a=2.0, b=5.0):
    time.sleep(random.uniform(a, b))


def homepage(url: str) -> str:
    """Strip path/query from a URL and return only the root homepage."""
    m = re.match(r'(https?://[^/]+)', url)
    return m.group(1) if m else url


def safe_get(url: str, timeout: int = 12) -> Optional[requests.Response]:
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r
    except Exception:
        pass
    return None


def ddg(query: str, n: int = 8) -> list:
    try:
        with DDGS() as d:
            return list(d.text(query, max_results=n))
    except Exception as e:
        print(f"    [ddg] {e}")
        delay(3, 6)
        return []


def claude(prompt: str, max_tokens: int = 300) -> str:
    """Call Claude Haiku; return the text or empty string."""
    if not HAS_CLAUDE:
        return ""
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"    [claude] {e}")
        return ""


# ── STEP 1: Amazon ────────────────────────────────────────────────────────────
def _amazon_organic_urls(brand: str) -> list[str]:
    """
    Use DDG to find real Amazon product listing URLs for this brand.
    DDG bypasses Amazon's bot-detection on the search page.
    """
    hits = ddg(f'site:amazon.com/dp "{brand}"', n=10)
    urls = []
    for h in hits:
        u = h.get("href", "")
        if "amazon.com" in u and "/dp/" in u:
            urls.append(u)
    return urls


def _parse_sold_by(html: str) -> tuple[str, str]:
    """
    From an Amazon product page, return (seller_name, seller_page_url).
    seller_page_url is the link we follow to get Detailed Seller Information.
    """
    soup = BeautifulSoup(html, "lxml")

    # Approach 1: #sellerProfileTriggerId link
    tag = soup.select_one("#sellerProfileTriggerId")
    if tag:
        name = tag.get_text(strip=True)
        href = tag.get("href", "")
        if href and name and "Amazon" not in name:
            url = ("https://www.amazon.com" + href) if href.startswith("/") else href
            return name, url

    # Approach 2: merchant-info section
    for a in soup.select("#merchantInfoFeature_feature_div a, #tabular-buybox a"):
        name = a.get_text(strip=True)
        href = a.get("href", "")
        if name and "Amazon" not in name and href:
            url = ("https://www.amazon.com" + href) if href.startswith("/") else href
            return name, url

    # Approach 3: regex on raw HTML
    m = re.search(r'[Ss]old by[:\s]*<[^>]+>([^<]{2,60})</a>', html)
    if m:
        return m.group(1).strip(), ""

    return "", ""


def _parse_seller_page(html: str) -> tuple[str, str]:
    """
    From the Amazon seller storefront page, extract Business Name + Address
    from the 'Detailed Seller Information' section.
    Returns (business_name, business_address).
    """
    soup = BeautifulSoup(html, "lxml")
    business_name = ""
    address_lines = []

    # The section is usually a <ul> or <div> with these labels
    text = soup.get_text("\n")
    bn = re.search(r'Business Name[:\s]*([^\n]{2,80})', text)
    if bn:
        business_name = bn.group(1).strip()

    ba = re.search(
        r'Business Address[:\s]*\n((?:[^\n]+\n){1,6})',
        text
    )
    if ba:
        lines = [l.strip() for l in ba.group(1).splitlines() if l.strip()]
        address_lines = lines

    return business_name, "\n".join(address_lines)


def amazon_get_seller_info(brand: str) -> dict:
    """Full Amazon flow: search → product page → seller page → business info."""
    out = {
        "amazon_search_url": f"https://www.amazon.com/s?k={brand.replace(' ', '+')}",
        "amazon_product_url": NO_INFO,
        "amazon_seller_name": NO_INFO,
        "amazon_seller_address": NO_INFO,
    }

    product_urls = _amazon_organic_urls(brand)
    if not product_urls:
        print(f"    [amazon] No product listings found via DDG")
        return out

    for product_url in product_urls[:3]:
        out["amazon_product_url"] = product_url
        print(f"    [amazon] Fetching product: {product_url[:70]}...")
        delay(2, 4)
        resp = safe_get(product_url)
        if not resp:
            print(f"    [amazon] Blocked / failed")
            continue

        seller_name, seller_page_url = _parse_sold_by(resp.text)
        if not seller_name:
            print(f"    [amazon] Could not find 'Sold by' on this listing, trying next")
            continue

        print(f"    [amazon] Sold by: {seller_name}")
        out["amazon_seller_name"] = seller_name

        if seller_page_url:
            delay(2, 3)
            print(f"    [amazon] Fetching seller page...")
            seller_resp = safe_get(seller_page_url)
            if seller_resp:
                biz_name, biz_addr = _parse_seller_page(seller_resp.text)
                if biz_name:
                    out["amazon_seller_name"] = biz_name
                    print(f"    [amazon] Business Name: {biz_name}")
                if biz_addr:
                    out["amazon_seller_address"] = biz_addr
                    print(f"    [amazon] Address found")
        break

    return out


# ── STEP 2: Official website ──────────────────────────────────────────────────
def find_official_website(brand: str, business_name: str) -> str:
    """
    Search DDG with brand name and/or business name.
    Return ONLY the homepage (root domain), validated by Claude if available.
    Skip Amazon, social media, and marketplace results.
    """
    skip = [
        "amazon.", "wikipedia.", "facebook.", "instagram.", "twitter.",
        "linkedin.", "yelp.", "reddit.", "youtube.", "tiktok.",
        "pinterest.", "walmart.", "ebay.", "etsy.", "shopify."
    ]
    candidates = []
    search_names = list(dict.fromkeys(filter(None, [brand, business_name])))

    for name in search_names:
        for q in [f'"{name}" official website', f'"{name}" site homepage']:
            for h in ddg(q, n=8):
                url = h.get("href", "")
                if url and not any(s in url for s in skip):
                    candidates.append(homepage(url))
            if candidates:
                break
        if candidates:
            break

    if not candidates:
        return NO_INFO

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    best = unique[0]

    # Ask Claude to pick the right one and confirm it's a homepage
    if HAS_CLAUDE and unique:
        names_str = " / ".join(search_names)
        options = "\n".join(f"{i+1}. {u}" for i, u in enumerate(unique[:5]))
        answer = claude(
            f'Brand: "{names_str}"\n'
            f'These are candidate homepages found online:\n{options}\n\n'
            f'Which URL is the correct official brand homepage that sells the same '
            f'products as the brand? Reply with ONLY the URL (e.g. https://example.com). '
            f'If none look correct, reply: No Info'
        )
        if answer.startswith("http"):
            best = homepage(answer)

    return best if best else NO_INFO


# ── STEP 3: Contact info from official website ────────────────────────────────
def find_contact_info(website_url: str) -> tuple[str, str]:
    """
    Try common contact page paths. Extract email address or contact form URL.
    Returns (email, form_url) — either/both may be NO_INFO.
    """
    if website_url == NO_INFO:
        return NO_INFO, NO_INFO

    base = website_url.rstrip("/")
    contact_paths = [
        "/contact", "/contact-us", "/pages/contact", "/pages/contact-us",
        "/support", "/help", "/about/contact",
    ]

    email_re = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
    skip_emails = {"example.com", "sentry.io", "schema.org", "w3.org"}

    found_email = NO_INFO
    found_form = NO_INFO

    for path in contact_paths:
        url = base + path
        resp = safe_get(url)
        if not resp:
            continue

        # Look for an email address
        emails = email_re.findall(resp.text)
        for em in emails:
            domain = em.split("@")[-1].lower()
            if domain not in skip_emails and not em.endswith(".png"):
                found_email = em
                print(f"    [contact] Email: {em}")
                break

        # Look for a <form> on the page (contact form)
        if found_email == NO_INFO:
            soup = BeautifulSoup(resp.text, "lxml")
            if soup.find("form"):
                found_form = url
                print(f"    [contact] Form: {url}")

        if found_email != NO_INFO or found_form != NO_INFO:
            break

        delay(1, 2)

    return found_email, found_form


# ── STEP 4: USPTO trademark owner ────────────────────────────────────────────
def uspto_find_owner(brand: str, business_name: str) -> dict:
    """
    Query the free USPTO EFTS API. Search by business name first, then brand.
    Extract owner first name + last name.
    USPTO does NOT expose personal emails — that field always returns No Info.
    """
    out = {
        "trademark_owner_first_name": NO_INFO,
        "trademark_owner_last_name": NO_INFO,
        "trademark_owner_email": NO_INFO,
    }

    search_terms = list(dict.fromkeys(filter(None, [business_name, brand])))

    for term in search_terms:
        if term == NO_INFO:
            continue
        try:
            resp = requests.get(
                "https://efts.uspto.gov/LATEST/search-efts",
                params={"q": f'"{term}"', "df": "mark_identification", "f": "json"},
                headers=BROWSER_HEADERS,
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            hits = (resp.json().get("hits") or {}).get("hits") or []
        except Exception as e:
            print(f"    [uspto] {e}")
            continue

        if not hits:
            continue

        source = hits[0].get("_source", {})
        owner_raw = source.get("owner") or ""
        if isinstance(owner_raw, list):
            owner_raw = owner_raw[0] if owner_raw else {}
        if isinstance(owner_raw, dict):
            owner_str = (
                owner_raw.get("owner_name") or
                owner_raw.get("party_name") or
                owner_raw.get("name") or ""
            )
        else:
            owner_str = str(owner_raw)

        owner_str = owner_str.strip()
        if not owner_str:
            continue

        print(f"    [uspto] Owner entity: {owner_str}")

        # Ask Claude whether this is a person's name and split it
        if HAS_CLAUDE:
            answer = claude(
                f'Trademark owner string: "{owner_str}"\n\n'
                f'If this is a real person\'s full name (not a company/LLC/Inc/Ltd/Corp/FZE), '
                f'reply with JSON: {{"first": "First", "last": "Last"}}\n'
                f'If it is a company name, reply with exactly: COMPANY'
            )
            if answer.startswith("{"):
                try:
                    parsed = json.loads(re.search(r'\{.*\}', answer, re.DOTALL).group())
                    out["trademark_owner_first_name"] = parsed.get("first", NO_INFO) or NO_INFO
                    out["trademark_owner_last_name"] = parsed.get("last", NO_INFO) or NO_INFO
                    print(f"    [uspto] Owner: {out['trademark_owner_first_name']} {out['trademark_owner_last_name']}")
                    break
                except Exception:
                    pass
        # If no Claude, try a simple heuristic: no LLC/Inc/Ltd in name
        elif not any(kw in owner_str.upper() for kw in ["LLC", "INC", "LTD", "CORP", "CO.", "FZE", "GMBH"]):
            parts = owner_str.split()
            if len(parts) >= 2:
                out["trademark_owner_first_name"] = parts[0]
                out["trademark_owner_last_name"] = " ".join(parts[1:])
                break

        # It's a company — still report entity in notes but keep names as No Info
        break

    return out


# ── STEP 5: Prospeo — LinkedIn + email for the owner ─────────────────────────
def _prospeo_post(path: str, body: dict) -> Optional[dict]:
    try:
        resp = requests.post(
            f"{PROSPEO_URL}{path}",
            headers={"Content-Type": "application/json", "X-KEY": PROSPEO_API_KEY},
            json=body,
            timeout=20,
        )
        data = resp.json()
        if resp.status_code != 200 or data.get("error"):
            msg = data.get("error_toast") or data.get("message") or resp.status_code
            print(f"    [prospeo] {path} → {msg}")
            return None
        return data
    except Exception as e:
        print(f"    [prospeo] {path} failed: {e}")
        return None


# Owner-type titles used when searching Prospeo by company/brand
OWNER_TITLES = ["Founder", "Owner", "Co-Founder", "Co-Owner", "President", "CEO"]


def _domain_from_url(url: str) -> str:
    d = re.sub(r'https?://(www\.)?', '', url)
    return d.split('/')[0].split('?')[0].lower()


def _prospeo_pick_person(people: list) -> Optional[dict]:
    """Prefer a person whose current title is owner-type; else take the first."""
    for person in people:
        p = person.get("person", person)
        title = (p.get("current_job_title") or "").lower()
        if any(t.lower() in title for t in OWNER_TITLES):
            return p
    if people:
        return people[0].get("person", people[0])
    return None


def prospeo_enrich_owner(first: str, last: str, company: str,
                         website: str, brand: str) -> dict:
    """
    Find the owner on Prospeo and enrich for LinkedIn + email.
    Search strategies, in priority order:
      1. By owner name (from USPTO), optionally scoped to the company
      2. By the official website's domain, filtered to owner-type titles
      3. By the brand name (as company), filtered to owner-type titles
    Returns found_first/found_last so the caller can backfill the owner name
    when USPTO didn't provide one.
    """
    out = {
        "owner_linkedin_url": NO_INFO,
        "owner_email_prospeo": NO_INFO,
        "found_first": NO_INFO,
        "found_last": NO_INFO,
    }

    person = None

    # Strategy 1: by owner name
    if first != NO_INFO and last != NO_INFO:
        full_name = f"{first} {last}"
        body: dict = {"page": 1, "filters": {"person_name": {"include": [full_name]}}}
        if company and company != NO_INFO:
            body["filters"]["company"] = {"names": {"include": [company]}}
        data = _prospeo_post("/search-person", body)
        if not data:
            data = _prospeo_post("/search-person", {
                "page": 1, "filters": {"person_name": {"include": [full_name]}},
            })
        person = _prospeo_pick_person((data or {}).get("results") or [])
        if person:
            print(f"    [prospeo] Matched by owner name: {full_name}")

    # Strategy 2: by the official website's domain
    if not person and website and website != NO_INFO:
        domain = _domain_from_url(website)
        print(f"    [prospeo] Searching by website domain: {domain}")
        data = _prospeo_post("/search-person", {
            "page": 1,
            "filters": {
                "company": {"domains": {"include": [domain]}},
                "person_job_title": {"include": OWNER_TITLES},
            },
        })
        person = _prospeo_pick_person((data or {}).get("results") or [])

    # Strategy 3: by brand name as company
    if not person and brand:
        print(f"    [prospeo] Searching by brand name: {brand}")
        data = _prospeo_post("/search-person", {
            "page": 1,
            "filters": {
                "company": {"names": {"include": [brand]}},
                "person_job_title": {"include": OWNER_TITLES},
            },
        })
        person = _prospeo_pick_person((data or {}).get("results") or [])

    if not person:
        print(f"    [prospeo] No person found")
        return out

    out["found_first"] = person.get("first_name") or NO_INFO
    out["found_last"] = person.get("last_name") or NO_INFO
    found_name = person.get("full_name") or f"{out['found_first']} {out['found_last']}"
    print(f"    [prospeo] Person: {found_name}")

    linkedin = person.get("linkedin_url") or ""
    person_id = person.get("person_id") or ""

    if linkedin:
        out["owner_linkedin_url"] = linkedin.split("?")[0]
        print(f"    [prospeo] LinkedIn: {out['owner_linkedin_url']}")

    # Enrich for email
    enrich_body = {}
    if linkedin:
        enrich_body = {"linkedin_url": linkedin}
    elif person_id:
        enrich_body = {"person_id": person_id}

    if enrich_body:
        delay(1, 2)
        edata = _prospeo_post("/enrich-person", enrich_body)
        if edata:
            resp_obj = edata.get("response", edata)
            email_f = resp_obj.get("email")
            if isinstance(email_f, dict):
                email = email_f.get("email") or email_f.get("value") or ""
            else:
                email = email_f or ""
            if not email:
                emails = resp_obj.get("emails") or []
                email = (emails[0].get("email") if isinstance(emails[0], dict) else emails[0]) if emails else ""
            if email:
                out["owner_email_prospeo"] = email
                print(f"    [prospeo] Email: {email}")

    return out


# ── Save ──────────────────────────────────────────────────────────────────────
CSV_FIELDS = [
    "input_brand",
    "amazon_search_url", "amazon_product_url",
    "amazon_seller_name", "amazon_seller_address",
    "official_website",
    "contact_email", "contact_form_url",
    "trademark_owner_first_name", "trademark_owner_last_name", "trademark_owner_email",
    "owner_linkedin_url", "owner_email_prospeo",
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


# ── Main pipeline ─────────────────────────────────────────────────────────────
def process_brands(brands: list, output_file: str = "results.json") -> list:
    results = []

    for i, brand in enumerate(brands, 1):
        brand = brand.strip()
        if not brand:
            continue

        print(f"\n{'='*60}")
        print(f"[{i}/{len(brands)}] {brand}")
        print(f"{'='*60}")
        r = BrandResult(input_brand=brand)

        # ── 1. Amazon ──────────────────────────────────────────────
        print(f"  STEP 1: Amazon seller lookup")
        seller = amazon_get_seller_info(brand)
        r.amazon_search_url   = seller["amazon_search_url"]
        r.amazon_product_url  = seller["amazon_product_url"]
        r.amazon_seller_name  = seller["amazon_seller_name"]
        r.amazon_seller_address = seller["amazon_seller_address"]

        # ── 2. Official website ────────────────────────────────────
        print(f"  STEP 2: Official website")
        biz = r.amazon_seller_name if r.amazon_seller_name != NO_INFO else ""
        website = find_official_website(brand, biz)
        r.official_website = website
        print(f"    → {website}")

        # ── 3. Contact info ────────────────────────────────────────
        if website != NO_INFO:
            print(f"  STEP 3: Contact info")
            delay(1, 2)
            r.contact_email, r.contact_form_url = find_contact_info(website)

        # ── 4. USPTO ───────────────────────────────────────────────
        print(f"  STEP 4: USPTO trademark owner")
        delay(1, 2)
        tm = uspto_find_owner(brand, biz)
        r.trademark_owner_first_name = tm["trademark_owner_first_name"]
        r.trademark_owner_last_name  = tm["trademark_owner_last_name"]
        r.trademark_owner_email      = tm["trademark_owner_email"]

        # ── 5. Prospeo: by owner name → website domain → brand ─────
        if (r.trademark_owner_first_name != NO_INFO
                or r.official_website != NO_INFO
                or brand):
            print(f"  STEP 5: Prospeo enrichment")
            delay(1, 2)
            pe = prospeo_enrich_owner(
                r.trademark_owner_first_name,
                r.trademark_owner_last_name,
                biz,
                r.official_website,
                brand,
            )
            r.owner_linkedin_url  = pe["owner_linkedin_url"]
            r.owner_email_prospeo = pe["owner_email_prospeo"]
            # Backfill the owner name from Prospeo when USPTO found none
            if r.trademark_owner_first_name == NO_INFO and pe["found_first"] != NO_INFO:
                r.trademark_owner_first_name = pe["found_first"]
                r.trademark_owner_last_name  = pe["found_last"]
                r.notes = (r.notes + "; owner name via Prospeo").strip("; ")
        else:
            print(f"  STEP 5: Skipping Prospeo (no name, website, or brand)")

        results.append(r)
        save_results(results, output_file)
        print(f"\n  ✓ Saved after brand {i}")

        if i < len(brands):
            delay(3, 6)

    return results


def print_summary(results: list):
    print(f"\n{'='*70}\nRESULTS SUMMARY\n{'='*70}")
    for r in results:
        print(f"\n  Brand           : {r.input_brand}")
        print(f"  Seller name     : {r.amazon_seller_name}")
        print(f"  Seller address  : {r.amazon_seller_address}")
        print(f"  Official website: {r.official_website}")
        print(f"  Contact email   : {r.contact_email}")
        print(f"  Contact form    : {r.contact_form_url}")
        print(f"  Owner           : {r.trademark_owner_first_name} {r.trademark_owner_last_name}")
        print(f"  Owner LinkedIn  : {r.owner_linkedin_url}")
        print(f"  Owner email     : {r.owner_email_prospeo}")


def main():
    parser = argparse.ArgumentParser(description="Brand enrichment pipeline")
    parser.add_argument("brands", nargs="*")
    parser.add_argument("--file", "-f")
    parser.add_argument("--output", "-o", default="results.json")
    args = parser.parse_args()

    brands = list(args.brands)
    if args.file:
        with open(args.file) as f:
            brands += [l.strip() for l in f if l.strip()]
    if not brands:
        print("No brands. Example: python3 brand_finder.py 'Americanflat'")
        sys.exit(1)

    print(f"Processing {len(brands)} brand(s)...")
    results = process_brands(brands, output_file=args.output)
    print_summary(results)
    csv_file = args.output.replace(".json", ".csv")
    print(f"\nSaved to    : {args.output}")
    print(f"Spreadsheet : {csv_file}  ← open in Excel or Numbers")


if __name__ == "__main__":
    main()
