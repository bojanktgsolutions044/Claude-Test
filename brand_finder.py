#!/usr/bin/env python3
"""
Brand Finder: Given a list of brand names, finds their official website
and generates a direct Amazon search link for each brand.
If no website is found, falls back to scraping Amazon for seller business info.
"""

import os
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

APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")

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
    contact_name: str = ""
    contact_title: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    website_contact_email: str = ""
    website_contact_phone: str = ""
    company_linkedin_url: str = ""


def random_delay(min_s=2.0, max_s=5.0):
    time.sleep(random.uniform(min_s, max_s))


def make_amazon_url(brand_name: str) -> str:
    query = brand_name.strip().replace(" ", "+")
    return f"https://www.amazon.com/s?k={query}"


def clean_url(url: str) -> str:
    """Strip tracking query parameters and return just the base URL."""
    return re.sub(r'\?.*$', '', url).rstrip('/')


def homepage_of(url: str) -> str:
    """Return just the scheme + domain (the clean homepage) of a URL."""
    m = re.match(r'(https?://[^/]+)', url)
    return m.group(1) if m else clean_url(url)


# Common words to ignore when extracting product keywords from Amazon titles
STOPWORDS = {
    "the", "and", "for", "with", "set", "pack", "of", "pcs", "pieces", "piece",
    "inch", "inches", "size", "color", "black", "white", "gold", "silver", "blue",
    "red", "green", "pink", "gray", "grey", "brown", "clear", "large", "small",
    "medium", "new", "premium", "quality", "best", "pro", "plus", "kit", "count",
    "x", "in", "to", "by", "or", "a", "an", "your", "our", "all", "each",
}


def get_amazon_product_keywords(brand_name: str, max_titles: int = 6) -> list:
    """
    Fetch the Amazon search page for a brand and extract the product-type
    keywords from the organic listing titles (e.g. "picture", "frame").
    These describe the brand's *industry* so we can match the right website.
    """
    if not HAS_REQUESTS:
        return []

    try:
        resp = requests.get(make_amazon_url(brand_name), headers=amazon_headers(), timeout=15)
        if resp.status_code != 200:
            return []
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    result_divs = soup.find_all("div", attrs={"data-component-type": "s-search-result"})

    brand_words = {re.sub(r'[^a-z0-9]', '', w.lower()) for w in brand_name.split()}
    counts = {}
    titles_used = 0

    for div in result_divs:
        if titles_used >= max_titles:
            break
        # Skip sponsored listings
        if div.find(lambda tag: tag.name in ("span", "a") and "Sponsored" in tag.get_text()):
            continue
        h2 = div.find("h2")
        title = h2.get_text(" ", strip=True) if h2 else ""
        if not title:
            continue
        titles_used += 1
        for raw in re.findall(r"[a-zA-Z]+", title.lower()):
            if len(raw) < 4 or raw in STOPWORDS or raw in brand_words:
                continue
            counts[raw] = counts.get(raw, 0) + 1

    # Keep keywords that show up in more than one listing (the real product type)
    keywords = [w for w, c in sorted(counts.items(), key=lambda x: -x[1]) if c >= 2]
    if not keywords:  # fall back to single-occurrence words if nothing repeats
        keywords = [w for w, _ in sorted(counts.items(), key=lambda x: -x[1])]
    return keywords[:8]


def page_keyword_score(url: str, keywords: list) -> int:
    """Fetch a candidate website and count how many product keywords it mentions."""
    if not HAS_REQUESTS or not keywords:
        return 0
    try:
        resp = requests.get(url, headers=amazon_headers(), timeout=12)
        if resp.status_code != 200:
            return 0
    except Exception:
        return 0
    text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True).lower()
    return sum(1 for kw in keywords if kw in text)


# Signals that a website is actually selling products (e-commerce store)
ECOMMERCE_SIGNALS = [
    "add to cart", "add to bag", "buy now", "shop now", "checkout",
    "in stock", "out of stock", "free shipping", "view cart",
    "add_to_cart", "shopify", "woocommerce", "product", "shop",
]


def website_sells_products(url: str, product_keywords: list) -> tuple:
    """
    Check if a website is an active e-commerce store selling the right products.
    Returns (has_store: bool, product_score: int).
    """
    if not HAS_REQUESTS:
        return False, 0
    try:
        resp = requests.get(url, headers=amazon_headers(), timeout=12)
        if resp.status_code != 200:
            return False, 0
    except Exception:
        return False, 0

    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    html = resp.text.lower()

    # Check for e-commerce signals in text or HTML
    has_store = any(signal in text or signal in html for signal in ECOMMERCE_SIGNALS)
    product_score = sum(1 for kw in product_keywords if kw in text)
    return has_store, product_score


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
    # Presentation / document / Q&A junk that sometimes ranks for odd names
    skip_domains += [
        "prezi.", "slideshare.", "scribd.", "issuu.", "quora.",
        "medium.com", "blogspot.", "wordpress.com", "ktiv.",
        "seovip.", "alibaba.", "aliexpress.", "made-in-china.",
        "dhgate.", "tradeindia.", "indiamart.",
    ]

    queries = [
        f'"{brand_name}" official website',
        f"{brand_name} brand official site -wikipedia -amazon",
    ]

    brand_slug = re.sub(r'[^a-z0-9]', '', brand_name.lower())
    # Build word tokens for matching (e.g. "Baby Sense" → ["baby", "sense"])
    brand_words = [w.lower() for w in brand_name.split() if len(w) > 2]

    def is_bad_url(url):
        if any(d in url for d in skip_domains):
            return True
        if any(p in url.lower() for p in skip_url_patterns):
            return True
        return False

    # What product type does this brand sell on Amazon? (e.g. picture frames)
    product_keywords = get_amazon_product_keywords(brand_name)
    if product_keywords:
        print(f"  Amazon product type: {', '.join(product_keywords[:5])}")
        # Add a product-aware query so generic brand names (e.g. "MCS") surface
        # the right niche site (e.g. mcsindustries.com) rather than unrelated ones
        top_products = " ".join(product_keywords[:3])
        queries.append(f"{brand_name} {top_products} official website")
        queries.append(f'"{brand_name}" {top_products} brand site')

    # Collect candidate websites from all queries
    candidates = []  # list of (url, domain)
    seen_domains = set()
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

        for r in results:
            url = r.get("href", "")
            if not url or is_bad_url(url):
                continue
            domain = re.sub(r'https?://(www\.)?', '', url).split('/')[0].lower()
            domain_clean = re.sub(r'[^a-z0-9]', '', domain)
            if domain in seen_domains:
                continue
            # Domain must relate to the brand, else ignore entirely
            if brand_slug not in domain_clean and not any(w in domain_clean for w in brand_words):
                continue
            seen_domains.add(domain)
            candidates.append((url, domain))
        random_delay(2, 3)

    if not candidates:
        return ""

    # Foreign country TLDs we de-prioritize (prefer global .com for US Amazon)
    foreign_tlds = (".de", ".fr", ".es", ".it", ".nl", ".au", ".ca", ".co.uk",
                    ".uk", ".jp", ".cn", ".in", ".br", ".mx", ".ru", ".pl")

    def domain_score(domain: str) -> int:
        """Score how 'official' a domain looks for this brand (higher = better)."""
        parts = domain.split(".")
        core = parts[0]  # the part before the first dot
        core_clean = re.sub(r'[^a-z0-9]', '', core)
        domain_clean = re.sub(r'[^a-z0-9]', '', domain)
        score = 0

        # Exact brand-name domain is the strongest signal
        if core_clean == brand_slug:
            score += 14
        elif brand_slug and brand_slug in domain_clean:
            score += 9
        # Reward each brand word present (e.g. "better" + "office")
        score += sum(3 for w in brand_words if w in domain_clean)

        # TLD preferences
        if domain.endswith(".com"):
            score += 3
        elif domain.endswith((".co", ".net", ".store", ".shop", ".org")):
            score += 1
        if domain.endswith(foreign_tlds):
            score -= 3

        # Penalize spammy / subdomain-on-unrelated-host domains
        if domain.count(".") >= 3:        # e.g. bsrhome.com.seovip.biz
            score -= 8
        return score

    # Score each candidate: check if it's an actual product-selling store,
    # then rank by domain relevance and product category match.
    scored = []
    for url, domain in candidates:
        home = homepage_of(url)
        has_store, p_score = website_sells_products(home, product_keywords)
        d_score = domain_score(domain)
        status = "has store" if has_store else "no store"
        if product_keywords:
            print(f"    candidate {home}  (domain {d_score}, product match {p_score}, {status})")
        # Only accept sites that are actual e-commerce stores selling related products.
        # If product keywords exist, require at least 1 keyword match.
        if not has_store:
            continue
        if product_keywords and p_score == 0:
            continue
        scored.append((d_score, p_score, home, domain))
        random_delay(1, 2)

    if not scored:
        return ""  # will display as "No Website"

    # Deterministic ranking: domain score, then product match, then prefer
    # .com, then shorter domain — so runs are consistent.
    scored.sort(key=lambda c: (
        c[0],                       # domain score
        min(c[1], 4),               # product match (capped so it can't dominate)
        c[3].endswith(".com"),      # prefer .com
        -len(c[3]),                 # prefer shorter domain
    ), reverse=True)

    return scored[0][2]


PHONE_PATTERN = re.compile(
    r'(\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b'
)
CONTACT_PAGE_PATHS = ["/contact", "/contact-us", "/pages/contact", "/pages/contact-us", "/contacts"]


def find_website_contact_info(website: str) -> tuple:
    """
    Visit the brand's website (and common contact-page paths) looking for
    a public email address (mailto: link) and phone number.
    Returns (email, phone) — either may be "" if not found.
    """
    if not HAS_REQUESTS or not website:
        return "", ""

    pages_to_try = [website] + [website.rstrip("/") + p for p in CONTACT_PAGE_PATHS]

    for page_url in pages_to_try:
        try:
            resp = requests.get(page_url, headers=amazon_headers(), timeout=12)
            if resp.status_code != 200:
                continue
        except Exception:
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        email = ""
        mailto = soup.find("a", href=re.compile(r"^mailto:", re.I))
        if mailto:
            email = mailto["href"].split(":", 1)[1].split("?")[0].strip()

        phone = ""
        tel = soup.find("a", href=re.compile(r"^tel:", re.I))
        if tel:
            phone = tel["href"].split(":", 1)[1].strip()
        else:
            text = soup.get_text(" ", strip=True)
            match = PHONE_PATTERN.search(text)
            if match:
                phone = match.group().strip()

        if email or phone:
            return email, phone

        random_delay(1, 2)

    return "", ""


def find_company_linkedin(brand_name: str) -> str:
    """Search for '{brand} LinkedIn' and return the first company LinkedIn page URL."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(f"{brand_name} LinkedIn", max_results=10))
    except Exception as e:
        print(f"  [linkedin search error] {e}")
        return ""

    for r in results:
        url = r.get("href", "")
        if "linkedin.com/company/" in url.lower():
            return clean_url(url)

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


SENIOR_TITLES = ["president", "ceo", "founder", "owner", "co-founder", "chief executive officer"]


def find_apollo_organization_id(website: str, brand_name: str) -> Optional[str]:
    """Look up an Apollo organization record by website domain (or brand name as fallback)."""
    if not APOLLO_API_KEY:
        return None

    headers = {"Content-Type": "application/json", "x-api-key": APOLLO_API_KEY}
    payload = {"page": 1, "per_page": 1}
    if website:
        domain = re.sub(r'https?://(www\.)?', '', website).split('/')[0]
        payload["q_organization_domains"] = domain
    else:
        payload["q_organization_name"] = brand_name

    try:
        resp = requests.post(
            "https://api.apollo.io/v1/organizations/search",
            headers=headers, json=payload, timeout=15,
        )
    except Exception as e:
        print(f"  [apollo] organizations/search failed: {e}")
        return None

    if resp.status_code in (401, 403, 429):
        print(f"  [apollo] organizations/search error {resp.status_code}: {resp.text[:300]}")
        return "OUT_OF_CREDITS"
    if resp.status_code != 200:
        print(f"  [apollo] organizations/search unexpected status {resp.status_code}: {resp.text[:300]}")
        return None

    orgs = resp.json().get("organizations", [])
    return orgs[0]["id"] if orgs else None


def find_apollo_contact(brand_name: str, website: str) -> dict:
    """
    Find a senior contact (president/CEO/founder/owner) for a brand via Apollo,
    including email and phone number. Returns {} if nothing found.
    """
    if not HAS_REQUESTS or not APOLLO_API_KEY:
        return {}

    org_id = find_apollo_organization_id(website, brand_name)
    if org_id == "OUT_OF_CREDITS":
        return {"_status": "Out of credits"}
    if not org_id:
        return {}

    headers = {"Content-Type": "application/json", "x-api-key": APOLLO_API_KEY}
    payload = {
        "organization_ids": [org_id],
        "person_titles": SENIOR_TITLES,
        "page": 1,
        "per_page": 1,
        "contact_email_status": ["verified", "guessed"],
    }

    try:
        resp = requests.post(
            "https://api.apollo.io/v1/mixed_people/search",
            headers=headers, json=payload, timeout=15,
        )
    except Exception as e:
        print(f"  [apollo] people search failed: {e}")
        return {}

    if resp.status_code in (401, 403, 429):
        print(f"  [apollo] mixed_people/search error {resp.status_code}: {resp.text[:300]}")
        return {"_status": "Out of credits"}
    if resp.status_code != 200:
        print(f"  [apollo] mixed_people/search unexpected status {resp.status_code}: {resp.text[:300]}")
        return {}

    people = resp.json().get("people", [])
    if not people:
        return {}

    person = people[0]
    email = person.get("email", "")
    if email == "email_not_unlocked@domain.com":
        email = "Out of credits"

    return {
        "contact_name": person.get("name", ""),
        "contact_title": person.get("title", ""),
        "contact_email": email,
        "contact_phone": (person.get("phone_numbers") or [{}])[0].get("raw_number", ""),
    }


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

            print(f"  Checking website for contact email/phone...")
            email, phone = find_website_contact_info(result.official_website)
            result.website_contact_email = email
            result.website_contact_phone = phone
            if email:
                print(f"  Website email : {email}")
            if phone:
                print(f"  Website phone : {phone}")

            print(f"  Searching for company LinkedIn page...")
            linkedin_url = find_company_linkedin(brand)
            result.company_linkedin_url = linkedin_url
            if linkedin_url:
                print(f"  LinkedIn      : {linkedin_url}")
            random_delay(2, 3)
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

        if APOLLO_API_KEY:
            print(f"  Looking up contact via Apollo...")
            contact = find_apollo_contact(brand, result.official_website)
            if contact.get("_status") == "Out of credits":
                result.contact_name = "Out of credits"
                result.contact_title = "Out of credits"
                result.contact_email = "Out of credits"
                result.contact_phone = "Out of credits"
            elif contact:
                result.contact_name = contact.get("contact_name", "")
                result.contact_title = contact.get("contact_title", "")
                result.contact_email = contact.get("contact_email", "")
                result.contact_phone = contact.get("contact_phone", "")
                print(f"  Contact found : {result.contact_name} ({result.contact_title})")
            else:
                print(f"  No contact found via Apollo.")

        results.append(result)

        with open(output_file, "w") as f:
            json.dump([asdict(r) for r in results], f, indent=2)

        csv_file = output_file.replace(".json", ".csv")
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "input_brand", "amazon_search_url", "official_website",
                "confidence", "notes", "seller_business_name", "seller_business_address",
                "contact_name", "contact_title", "contact_email", "contact_phone",
                "website_contact_email", "website_contact_phone", "company_linkedin_url"
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
        print(f"  Official site : {r.official_website or 'No Website'}")
        if r.seller_business_name:
            print(f"  Business name : {r.seller_business_name}")
        if r.seller_business_address:
            print(f"  Address       : {r.seller_business_address}")
        if r.contact_name:
            print(f"  Contact       : {r.contact_name} ({r.contact_title})")
            print(f"  Email         : {r.contact_email}")
            print(f"  Phone         : {r.contact_phone}")
        if r.website_contact_email:
            print(f"  Website email : {r.website_contact_email}")
        if r.website_contact_phone:
            print(f"  Website phone : {r.website_contact_phone}")
        if r.company_linkedin_url:
            print(f"  LinkedIn      : {r.company_linkedin_url}")
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
