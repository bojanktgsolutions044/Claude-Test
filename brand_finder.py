#!/usr/bin/env python3
"""
Brand Finder — full enrichment pipeline:
  1. Amazon: find organic listing (up to 10) → seller page → Business Name + Address
  2. Official website: homepage only, verified via Claude
  3. Contact: email or contact-form URL from the official site
  4. USPTO: search by Business Name → owner first/last name
  5. LinkedIn: company page + owner profile via DDG title searches
  6. Prospeo: verify/enrich owner contact (by domain, then brand)
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

# ── API keys ─────────────────────────────────────────────────────────────────
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

# Title priority for LinkedIn owner search (highest first)
OWNER_TITLE_SEARCHES = ["Founder", "Co-Founder", "Owner", "Co-Owner", "President", "CEO"]


# ── Data model ────────────────────────────────────────────────────────────────
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
    # LinkedIn (found via DDG searches)
    company_linkedin_url: str = NO_INFO
    owner_linkedin_url: str = NO_INFO
    owner_linkedin_title: str = NO_INFO   # which title query found the profile
    # Prospeo enrichment
    owner_email_prospeo: str = NO_INFO
    # Meta
    notes: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────
def delay(a=2.0, b=5.0):
    time.sleep(random.uniform(a, b))


def homepage(url: str) -> str:
    """Return only the root domain (https://example.com), strip path/query."""
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


def claude_ask(prompt: str, max_tokens: int = 300) -> str:
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


def is_us_address(address: str) -> bool:
    """
    Decide whether a business address is in the US.
    Returns True when the address looks US-based (or is unknown/empty, so we
    don't wrongly skip USPTO), and False when it clearly names another country.
    """
    if not address or address == NO_INFO:
        return True  # unknown — don't skip USPTO

    addr = address.strip()
    upper = addr.upper()

    # Strong US signals: trailing ", US" / ", USA" or a US ZIP near the end
    if re.search(r',\s*(US|USA|UNITED STATES)\b\.?\s*$', upper):
        return True
    if re.search(r'\b[A-Z]{2}\s*,?\s*\d{5}(-\d{4})?\b', upper):
        return True

    # Explicit non-US country codes/names (ISO + common spellings)
    non_us = [
        "CN", "CHINA", "HONG KONG", "HK", "UK", "UNITED KINGDOM", "GB",
        "DE", "GERMANY", "FR", "FRANCE", "IT", "ITALY", "ES", "SPAIN",
        "IN", "INDIA", "AE", "UAE", "UNITED ARAB EMIRATES", "CA", "CANADA",
        "AU", "AUSTRALIA", "JP", "JAPAN", "KR", "KOREA", "VN", "VIETNAM",
        "PL", "POLAND", "NL", "NETHERLANDS", "TR", "TURKEY", "MX", "MEXICO",
        "SE", "SWEDEN", "IE", "IRELAND", "TW", "TAIWAN", "SG", "SINGAPORE",
    ]
    # Check the final segment of the address (country usually comes last)
    last_part = upper.split(",")[-1].strip().rstrip(".")
    if last_part in non_us:
        return False
    # Non-Latin characters (e.g. Chinese) strongly imply non-US
    if re.search(r'[一-鿿぀-ヿ가-힯]', addr):
        return False

    return True  # default: treat as US-eligible


def _domain_from_url(url: str) -> str:
    d = re.sub(r'https?://(www\.)?', '', url)
    return d.split('/')[0].split('?')[0].lower()


# ── STEP 1: Amazon seller info ────────────────────────────────────────────────
def _amazon_product_urls(brand: str) -> list:
    """Find up to 10 Amazon /dp/ product URLs via DDG (avoids bot detection)."""
    urls = []
    queries = [
        f'site:amazon.com/dp "{brand}"',
        f'site:amazon.com "{brand}" buy',
        f'amazon.com "{brand}" product',
    ]
    seen = set()
    for q in queries:
        for h in ddg(q, n=10):
            u = h.get("href", "")
            if "amazon.com" in u and "/dp/" in u and u not in seen:
                seen.add(u)
                urls.append(u)
        if len(urls) >= 10:
            break
    return urls[:10]


def _similar(a: str, b: str) -> bool:
    """True if two names overlap by slug (one contained in the other)."""
    sa, sb = _slug(a), _slug(b)
    if not sa or not sb:
        return False
    return sa in sb or sb in sa


def _parse_brand_byline(html: str) -> str:
    """
    Read the product's BRAND from the Amazon byline, e.g.
      'Visit the Americanflat Store'  →  Americanflat
      'Brand: Americanflat'           →  Americanflat
    Returns '' if no byline present.
    """
    soup = BeautifulSoup(html, "lxml")
    el = soup.select_one("#bylineInfo, #brand, a#bylineInfo")
    if el:
        t = el.get_text(" ", strip=True)
        m = re.search(r'Visit the (.+?) Store', t, re.I)
        if m:
            return m.group(1).strip()
        m = re.search(r'Brand:\s*(.+)', t, re.I)
        if m:
            return m.group(1).strip()
        if t and "store" not in t.lower():
            return t.strip()
    # Fallback: "Brand <name>" inside the product detail table
    m = re.search(r'<th[^>]*>\s*Brand\s*</th>\s*<td[^>]*>\s*<span[^>]*>([^<]{2,40})</span>', html, re.I)
    if m:
        return m.group(1).strip()
    return ""


def _parse_sold_by(html: str) -> tuple:
    """
    Extract (seller_name, seller_page_url) from an Amazon product page.
    Tries multiple selectors to handle different page layouts.
    """
    soup = BeautifulSoup(html, "lxml")

    # Selector 1: #sellerProfileTriggerId (most common)
    tag = soup.select_one("#sellerProfileTriggerId")
    if tag:
        name = tag.get_text(strip=True)
        href = tag.get("href", "")
        if name and "Amazon" not in name and href:
            return name, ("https://www.amazon.com" + href if href.startswith("/") else href)

    # Selector 2: tabular buybox / merchant info
    for a in soup.select(
        "#merchantInfoFeature_feature_div a, "
        "#tabular-buybox a, "
        "#buybox-tabular a, "
        "#soldByThirdParty a"
    ):
        name = a.get_text(strip=True)
        href = a.get("href", "")
        if name and "Amazon" not in name and href and "seller=" in href:
            return name, ("https://www.amazon.com" + href if href.startswith("/") else href)

    # Selector 3: regex sweep on raw HTML
    m = re.search(
        r'"sellerName"\s*:\s*"([^"]{2,60})".*?"sellerURL"\s*:\s*"([^"]+)"',
        html, re.DOTALL
    )
    if m and "Amazon" not in m.group(1):
        url = m.group(2).replace("\\u0026", "&")
        return m.group(1), url

    m2 = re.search(r'[Ss]old by[:\s]*<[^>]+>([^<]{2,60})</a>', html)
    if m2:
        return m2.group(1).strip(), ""

    return "", ""


def _parse_seller_page(html: str) -> tuple:
    """Extract (business_name, business_address) from an Amazon seller page."""
    text = BeautifulSoup(html, "lxml").get_text("\n")
    biz_name = ""
    biz_addr = ""

    bn = re.search(r'Business [Nn]ame[:\s]*([^\n]{2,80})', text)
    if bn:
        biz_name = bn.group(1).strip()

    ba = re.search(r'Business [Aa]ddress[:\s]*\n((?:[^\n]+\n){1,8})', text)
    if ba:
        lines = [l.strip() for l in ba.group(1).splitlines() if l.strip()]
        biz_addr = ", ".join(lines)

    return biz_name, biz_addr


def amazon_get_seller_info(brand: str) -> dict:
    out = {
        "amazon_search_url": f"https://www.amazon.com/s?k={brand.replace(' ', '+')}",
        "amazon_product_url": NO_INFO,
        "amazon_seller_name": NO_INFO,
        "amazon_seller_address": NO_INFO,
    }

    product_urls = _amazon_product_urls(brand)
    if not product_urls:
        print(f"    [amazon] No listings found via DDG")
        return out

    print(f"    [amazon] Found {len(product_urls)} candidate listings, verifying brand...")

    for product_url in product_urls:
        print(f"    [amazon] → {product_url[:75]}...")
        delay(2, 4)
        resp = safe_get(product_url)
        if not resp:
            print(f"    [amazon]   Blocked/failed, skipping")
            continue

        # VERIFY this product actually belongs to the brand (byline brand check)
        product_brand = _parse_brand_byline(resp.text)
        if product_brand:
            if not _similar(product_brand, brand):
                print(f"    [amazon]   Brand mismatch (listing brand: '{product_brand}'), skipping")
                continue
            print(f"    [amazon]   Brand verified: {product_brand}")
        else:
            print(f"    [amazon]   No brand byline found, skipping (can't verify)")
            continue

        seller_name, seller_page_url = _parse_sold_by(resp.text)
        if not seller_name:
            print(f"    [amazon]   No 'Sold by' found, trying next listing")
            continue

        out["amazon_product_url"] = product_url
        out["amazon_seller_name"] = seller_name
        print(f"    [amazon]   Sold by: {seller_name}")

        if seller_page_url:
            delay(2, 3)
            print(f"    [amazon]   Fetching seller page...")
            seller_resp = safe_get(seller_page_url)
            if seller_resp:
                biz_name, biz_addr = _parse_seller_page(seller_resp.text)
                if biz_name:
                    out["amazon_seller_name"] = biz_name
                    print(f"    [amazon]   Business Name : {biz_name}")
                if biz_addr:
                    out["amazon_seller_address"] = biz_addr
                    print(f"    [amazon]   Business Addr : {biz_addr[:60]}...")
        break  # Got a brand-verified seller — stop

    if out["amazon_seller_name"] == NO_INFO:
        print(f"    [amazon] No brand-verified listing found")

    return out


# Sites that are NOT a brand's own homepage — social, marketplaces, and the
# data-broker / company-directory aggregators that often outrank the real site.
SKIP_SITE_DOMAINS = [
    "amazon.", "wikipedia.", "facebook.", "instagram.", "twitter.", "x.com",
    "linkedin.", "yelp.", "reddit.", "youtube.", "tiktok.", "pinterest.",
    "walmart.", "ebay.", "etsy.", "shopify.com", "aliexpress.",
    # data brokers / directories (these caused the zoominfo bug)
    "zoominfo.", "crunchbase.", "bloomberg.", "dnb.com", "dunandbradstreet.",
    "rocketreach.", "apollo.io", "pitchbook.", "owler.", "glassdoor.",
    "indeed.", "leadiq.", "signalhire.", "kompass.", "manta.",
    "bbb.org", "trustpilot.", "g2.com", "capterra.", "buzzfile.",
]


def _slug(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _page_mentions(url: str, terms: list) -> Optional[bool]:
    """
    Fetch a homepage and check whether it mentions any of the terms.
    Returns True/False, or None if the page couldn't be fetched.
    """
    resp = safe_get(url, timeout=10)
    if not resp:
        return None
    page_slug = _slug(BeautifulSoup(resp.text, "lxml").get_text(" "))
    for t in terms:
        if t and t != NO_INFO and _slug(t) and _slug(t) in page_slug:
            return True
    return False


def _candidate_domains(name: str) -> list:
    """Build likely homepage URLs from a brand/business name."""
    s = _slug(name)
    if not s or len(s) > 30:
        return []
    return [
        f"https://www.{s}.com", f"https://{s}.com",
        f"https://www.{s}s.com", f"https://{s}s.com",
        f"https://{s}.co", f"https://www.{s}.co",
    ]


def find_official_website(brand: str, business_name: str) -> str:
    """
    Find the brand's OWN homepage. A candidate is only accepted if its homepage
    actually mentions the brand or business name — this prevents picking
    look-alike domains (e.g. skysports.com for 'Amy Sport').
    Strategy A: guess the domain directly (amysport.com) and verify content.
    Strategy B: DuckDuckGo search + content verification.
    """
    search_names = list(dict.fromkeys(x for x in [brand, business_name] if x and x != NO_INFO))

    # ── Strategy A: direct domain guesses (no search engine needed) ──────────
    print(f"    [website] Trying direct domain guesses...")
    tried = set()
    for name in search_names:
        for guess in _candidate_domains(name):
            if guess in tried:
                continue
            tried.add(guess)
            if _page_mentions(guess, search_names) is True:
                print(f"    [website] Verified direct domain: {guess}")
                return homepage(guess)
            delay(0.5, 1.2)

    # ── Strategy B: DuckDuckGo search ────────────────────────────────────────
    candidates = []

    for name in search_names:
        for q in [f'{name} official website', f'"{name}" homepage', name]:
            for h in ddg(q, n=8):
                url = h.get("href", "")
                if url and not any(s in url for s in SKIP_SITE_DOMAINS):
                    candidates.append(homepage(url))
            if len(candidates) >= 6:
                break
        if len(candidates) >= 6:
            break

    if not candidates:
        return NO_INFO

    seen, unique = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c); unique.append(c)

    terms = search_names
    brand_slug = _slug(brand)
    biz_slug = _slug(business_name) if business_name and business_name != NO_INFO else ""

    # Rank: domains whose name matches the brand/business slug come first
    def rank(u):
        dslug = _slug(_domain_from_url(u).split(".")[0])
        if brand_slug and (brand_slug in dslug or dslug in brand_slug):
            return 0
        if biz_slug and (biz_slug in dslug or dslug in biz_slug):
            return 1
        return 2
    ordered = sorted(unique, key=rank)

    # Accept the first candidate whose homepage actually mentions the brand/biz
    fallback = None
    for u in ordered[:6]:
        verified = _page_mentions(u, terms)
        if verified is True:
            print(f"    [website] Verified (mentions brand): {u}")
            return u
        if verified is None and fallback is None and rank(u) == 0:
            fallback = u  # couldn't fetch, but domain strongly matches brand
        delay(1, 2)

    # Nothing verified by content. Only fall back to a strong brand-slug domain.
    if fallback:
        print(f"    [website] Unverified but brand-matched domain: {fallback}")
        return fallback

    print(f"    [website] No homepage could be verified for this brand")
    return NO_INFO


# ── STEP 3: Contact info ──────────────────────────────────────────────────────
def find_contact_info(website_url: str) -> tuple:
    if website_url == NO_INFO:
        return NO_INFO, NO_INFO

    base = website_url.rstrip("/")
    paths = [
        "/contact", "/contact-us", "/pages/contact", "/pages/contact-us",
        "/support", "/help", "/about/contact", "/about-us",
    ]
    email_re = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
    skip_domains = {"example.com", "sentry.io", "schema.org", "w3.org", "wixpress.com"}

    found_email = NO_INFO
    found_form = NO_INFO

    for path in paths:
        resp = safe_get(base + path)
        if not resp:
            continue
        for em in email_re.findall(resp.text):
            if em.split("@")[-1].lower() not in skip_domains and not em.endswith(".png"):
                found_email = em
                print(f"    [contact] Email: {em}")
                break
        if found_email == NO_INFO and BeautifulSoup(resp.text, "lxml").find("form"):
            found_form = base + path
            print(f"    [contact] Form: {found_form}")
        if found_email != NO_INFO or found_form != NO_INFO:
            break
        delay(1, 2)

    return found_email, found_form


# ── STEP 3b: Founder name from the brand's own website ───────────────────────
FOUNDER_PATTERNS = [
    r'founded\s+(?:in\s+\d{4}\s+)?by\s+([A-Z][a-zA-Z\'\-]+\s+[A-Z][a-zA-Z\'\-]+)',
    r'launched\s+(?:in\s+\d{4}\s+)?by\s+([A-Z][a-zA-Z\'\-]+\s+[A-Z][a-zA-Z\'\-]+)',
    r'started\s+(?:in\s+\d{4}\s+)?by\s+([A-Z][a-zA-Z\'\-]+\s+[A-Z][a-zA-Z\'\-]+)',
    r'created\s+by\s+([A-Z][a-zA-Z\'\-]+\s+[A-Z][a-zA-Z\'\-]+)',
    r'([A-Z][a-zA-Z\'\-]+\s+[A-Z][a-zA-Z\'\-]+),?\s+(?:the\s+)?(?:founder|co-founder|owner|ceo)\b',
]


def website_find_founder(website: str) -> tuple:
    """
    Scrape the brand's homepage + About pages for a founder statement like
    'launched in 2017 by Amy Lipton'. Returns (first, last) or (NO_INFO, NO_INFO).
    This is the most trustworthy owner source — the brand's own words.
    """
    if website == NO_INFO:
        return NO_INFO, NO_INFO

    base = website.rstrip("/")
    paths = ["", "/about", "/about-us", "/pages/about", "/pages/about-us",
             "/our-story", "/pages/our-story", "/pages/about-amy", "/story"]

    for path in paths:
        resp = safe_get(base + path)
        if not resp:
            continue
        text = BeautifulSoup(resp.text, "lxml").get_text(" ")
        text = re.sub(r'\s+', ' ', text)
        for pat in FOUNDER_PATTERNS:
            m = re.search(pat, text)
            if m:
                name = m.group(1).strip()
                # Avoid false hits like "Privacy Policy"
                if name.lower() in ("privacy policy", "terms service", "all rights"):
                    continue
                parts = name.split()
                if len(parts) >= 2:
                    print(f"    [website] Founder on site: {name} (from {path or '/'})")
                    return parts[0], " ".join(parts[1:])
        delay(1, 2)

    return NO_INFO, NO_INFO


# ── STEP 4: USPTO ─────────────────────────────────────────────────────────────
def uspto_find_owner(brand: str, business_name: str) -> dict:
    out = {
        "trademark_owner_first_name": NO_INFO,
        "trademark_owner_last_name": NO_INFO,
    }
    search_terms = list(dict.fromkeys(
        x for x in [business_name, brand] if x and x != NO_INFO
    ))
    for term in search_terms:
        try:
            resp = requests.get(
                "https://efts.uspto.gov/LATEST/search-efts",
                params={"q": f'"{term}"', "df": "mark_identification", "f": "json"},
                headers=BROWSER_HEADERS, timeout=15,
            )
            if resp.status_code != 200:
                continue
            hits = (resp.json().get("hits") or {}).get("hits") or []
        except Exception as e:
            print(f"    [uspto] {e}"); continue

        if not hits:
            continue

        source = hits[0].get("_source", {})
        owner_raw = source.get("owner") or ""
        if isinstance(owner_raw, list):
            owner_raw = owner_raw[0] if owner_raw else {}
        owner_str = (
            owner_raw.get("owner_name") or owner_raw.get("party_name") or owner_raw.get("name") or ""
            if isinstance(owner_raw, dict) else str(owner_raw)
        ).strip()

        if not owner_str:
            continue
        print(f"    [uspto] Owner entity: {owner_str}")

        if HAS_CLAUDE:
            answer = claude_ask(
                f'Trademark owner: "{owner_str}"\n'
                f'If this is a real person\'s full name (not a company), '
                f'reply JSON: {{"first":"...", "last":"..."}}\n'
                f'If it is a company, reply: COMPANY'
            )
            if answer.startswith("{"):
                try:
                    p = json.loads(re.search(r'\{.*\}', answer, re.DOTALL).group())
                    out["trademark_owner_first_name"] = p.get("first", NO_INFO) or NO_INFO
                    out["trademark_owner_last_name"]  = p.get("last", NO_INFO) or NO_INFO
                    print(f"    [uspto] Owner person: {out['trademark_owner_first_name']} {out['trademark_owner_last_name']}")
                    break
                except Exception:
                    pass
        elif not any(k in owner_str.upper() for k in ["LLC", "INC", "LTD", "CORP", "CO.", "FZE"]):
            parts = owner_str.split()
            if len(parts) >= 2:
                out["trademark_owner_first_name"] = parts[0]
                out["trademark_owner_last_name"]  = " ".join(parts[1:])
                break
        break

    return out


# ── STEP 5: LinkedIn via DDG ──────────────────────────────────────────────────
def _name_from_linkedin_title(title: str) -> tuple:
    """
    LinkedIn result titles look like:
      'Giorgio Piccoli - Founder - Americanflat | LinkedIn'
    Return (first, last) parsed from the part before the first ' - ' or '|'.
    """
    head = re.split(r'\s[-–|]\s', title.strip())[0].strip()
    head = re.sub(r'\s*\|\s*LinkedIn.*$', '', head, flags=re.I).strip()
    # Reject if it doesn't look like a person's name
    if not head or len(head.split()) < 2 or len(head) > 50:
        return NO_INFO, NO_INFO
    parts = head.split()
    return parts[0], " ".join(parts[1:])


def find_linkedin_urls(brand: str, business_name: str) -> dict:
    """
    A. Company page: search DDG, then validate the URL slug contains the brand.
    B. Owner page: search by each title in priority order; parse owner name from
       the result title. Owner name is ONLY taken from LinkedIn — never from Prospeo.
    """
    out = {
        "company_linkedin_url": NO_INFO,
        "owner_linkedin_url": NO_INFO,
        "owner_linkedin_title": NO_INFO,
        "owner_first": NO_INFO,
        "owner_last": NO_INFO,
    }
    search_name = business_name if (business_name and business_name != NO_INFO) else brand
    brand_slug = _slug(brand)

    # A) Company LinkedIn — must validate slug matches brand name
    print(f"    [linkedin] Searching company page...")
    queries = [
        f'"{search_name}" site:linkedin.com/company',
        f'"{brand}" site:linkedin.com/company',
        f'{brand} linkedin company profile',
    ]
    for q in queries:
        for h in ddg(q, n=8):
            url = h.get("href", "")
            if "linkedin.com/company/" not in url:
                continue
            # Extract the slug from the URL and check it matches the brand
            slug_match = re.search(r'linkedin\.com/company/([^/?]+)', url)
            if not slug_match:
                continue
            url_slug = _slug(slug_match.group(1))
            if brand_slug in url_slug or url_slug in brand_slug:
                out["company_linkedin_url"] = url.split("?")[0]
                print(f"    [linkedin] Company: {out['company_linkedin_url']}")
                break
        if out["company_linkedin_url"] != NO_INFO:
            break
    delay(1, 2)

    # B) Owner LinkedIn — title-priority search, parse name from result title
    print(f"    [linkedin] Searching owner profile by title...")
    for title in OWNER_TITLE_SEARCHES:
        q = f'"{brand}" {title} linkedin'
        for h in ddg(q, n=6):
            url = h.get("href", "")
            result_title = h.get("title", "")
            snippet = (h.get("body", "") + result_title).lower()
            if "linkedin.com/in/" not in url:
                continue
            if title.lower() not in snippet:
                continue
            out["owner_linkedin_url"] = url.split("?")[0]
            out["owner_linkedin_title"] = title
            first, last = _name_from_linkedin_title(result_title)
            out["owner_first"], out["owner_last"] = first, last
            name_str = f"{first} {last}" if first != NO_INFO else "(name not parsed)"
            print(f"    [linkedin] Owner ({title}): {name_str}")
            print(f"    [linkedin] Profile: {out['owner_linkedin_url']}")
            break
        if out["owner_linkedin_url"] != NO_INFO:
            break
        delay(1, 2)

    return out


# ── STEP 6: Prospeo ───────────────────────────────────────────────────────────
def _prospeo_post(path: str, body: dict) -> Optional[dict]:
    try:
        resp = requests.post(
            f"{PROSPEO_URL}{path}",
            headers={"Content-Type": "application/json", "X-KEY": PROSPEO_API_KEY},
            json=body, timeout=20,
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


def _pick_best_person(people: list) -> Optional[dict]:
    """Return the person with the most owner-like title; else first result."""
    for title in OWNER_TITLE_SEARCHES:
        for person in people:
            p = person.get("person", person)
            if title.lower() in (p.get("current_job_title") or "").lower():
                return p
    return people[0].get("person", people[0]) if people else None


def prospeo_get_email(
    known_first: str, known_last: str,
    owner_linkedin_url: str,
    brand: str, business_name: str,
) -> str:
    """
    Use Prospeo to find the owner's email ONLY.
    Owner identity is never determined here — only enrichment.

    Search order:
      1. Enrich directly via the LinkedIn URL we already found (most precise)
      2. Search by owner full name + company → enrich matched person
      3. Search by brand/business name + owner titles → enrich matched person
         (only accepted if the person's company name matches the brand)
    """
    # 1. Direct enrich via LinkedIn URL (fastest, most accurate)
    if owner_linkedin_url and owner_linkedin_url != NO_INFO:
        print(f"    [prospeo] Enriching via LinkedIn URL...")
        edata = _prospeo_post("/enrich-person", {"linkedin_url": owner_linkedin_url})
        email = _extract_email_from_enrich(edata)
        if email:
            return email

    # 2. Search by owner name
    if known_first != NO_INFO and known_last != NO_INFO:
        full_name = f"{known_first} {known_last}"
        body: dict = {"page": 1, "filters": {"person_name": {"include": [full_name]}}}
        if business_name and business_name != NO_INFO:
            body["filters"]["company"] = {"names": {"include": [business_name]}}
        data = _prospeo_post("/search-person", body)
        person = _pick_best_person((data or {}).get("results") or [])
        if person:
            print(f"    [prospeo] Matched by name: {full_name}")
            email = _enrich_person_for_email(person)
            if email:
                return email

    # 3. Search by brand/company — only accept if company name matches
    brand_slug = _slug(brand)
    for search_name in list(dict.fromkeys(
        x for x in [business_name, brand] if x and x != NO_INFO
    )):
        print(f"    [prospeo] Searching by company: {search_name}")
        data = _prospeo_post("/search-person", {
            "page": 1,
            "filters": {
                "company": {"names": {"include": [search_name]}},
                "person_job_title": {"include": OWNER_TITLE_SEARCHES},
            },
        })
        people = (data or {}).get("results") or []
        for candidate in people:
            p = candidate.get("person", candidate)
            # Verify the person's current company actually matches our brand
            current_company = ""
            for job in p.get("job_history", []):
                if job.get("current"):
                    current_company = job.get("company_name", "")
                    break
            if not current_company:
                current_company = p.get("headline", "")
            if brand_slug not in _slug(current_company):
                print(f"    [prospeo] Skipping {p.get('full_name')} — company mismatch ({current_company})")
                continue
            print(f"    [prospeo] Matched: {p.get('full_name')} at {current_company}")
            email = _enrich_person_for_email(p)
            if email:
                return email
            break

    print(f"    [prospeo] No email found")
    return NO_INFO


def _extract_email_from_enrich(edata: Optional[dict]) -> str:
    """Pull email string out of a /enrich-person response."""
    if not edata:
        return ""
    ro = edata.get("response", edata)
    ef = ro.get("email")
    if isinstance(ef, dict):
        email = ef.get("email") or ef.get("value") or ""
    else:
        email = ef or ""
    if not email:
        emails = ro.get("emails") or []
        if emails:
            email = emails[0].get("email", "") if isinstance(emails[0], dict) else emails[0]
    if email:
        print(f"    [prospeo] Email: {email}")
    return email


def _enrich_person_for_email(person: dict) -> str:
    """Given a person dict from search-person, enrich for email."""
    linkedin = person.get("linkedin_url") or ""
    person_id = person.get("person_id") or ""
    enrich_body = {}
    if linkedin:
        enrich_body = {"linkedin_url": linkedin}
    elif person_id:
        enrich_body = {"person_id": person_id}
    if not enrich_body:
        return ""
    delay(1, 2)
    edata = _prospeo_post("/enrich-person", enrich_body)
    return _extract_email_from_enrich(edata)


# ── Save ──────────────────────────────────────────────────────────────────────
CSV_FIELDS = [
    "input_brand",
    "amazon_search_url", "amazon_product_url",
    "amazon_seller_name", "amazon_seller_address",
    "official_website",
    "contact_email", "contact_form_url",
    "trademark_owner_first_name", "trademark_owner_last_name",
    "company_linkedin_url",
    "owner_linkedin_url", "owner_linkedin_title",
    "owner_email_prospeo",
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

        # 1. Amazon
        print(f"  STEP 1: Amazon seller lookup")
        seller = amazon_get_seller_info(brand)
        r.amazon_search_url     = seller["amazon_search_url"]
        r.amazon_product_url    = seller["amazon_product_url"]
        r.amazon_seller_name    = seller["amazon_seller_name"]
        r.amazon_seller_address = seller["amazon_seller_address"]
        biz = r.amazon_seller_name if r.amazon_seller_name != NO_INFO else ""

        # 2. Official website
        print(f"  STEP 2: Official website")
        r.official_website = find_official_website(brand, biz)
        print(f"    → {r.official_website}")

        # 3. Contact info + founder from the brand's own website
        if r.official_website != NO_INFO:
            print(f"  STEP 3: Contact info")
            delay(1, 2)
            r.contact_email, r.contact_form_url = find_contact_info(r.official_website)

            print(f"  STEP 3b: Founder from website")
            delay(1, 2)
            wf, wl = website_find_founder(r.official_website)
            if wf != NO_INFO:
                r.trademark_owner_first_name = wf
                r.trademark_owner_last_name  = wl
                r.notes = (r.notes + "; owner via website").lstrip("; ")

        # 4. USPTO — only for US-based businesses, and only if owner still unknown
        print(f"  STEP 4: USPTO trademark owner")
        if r.trademark_owner_first_name != NO_INFO:
            print(f"    [uspto] Skipped — owner already found")
        elif not is_us_address(r.amazon_seller_address):
            print(f"    [uspto] Skipped — business address is outside the US")
            r.notes = (r.notes + "; non-US seller, USPTO skipped").lstrip("; ")
        else:
            delay(1, 2)
            tm = uspto_find_owner(brand, biz)
            r.trademark_owner_first_name = tm["trademark_owner_first_name"]
            r.trademark_owner_last_name  = tm["trademark_owner_last_name"]

        # 5. LinkedIn — company page + owner profile via DDG title searches
        print(f"  STEP 5: LinkedIn search")
        delay(1, 2)
        li = find_linkedin_urls(brand, biz)
        r.company_linkedin_url = li["company_linkedin_url"]
        r.owner_linkedin_url   = li["owner_linkedin_url"]
        r.owner_linkedin_title = li["owner_linkedin_title"]

        # Owner name: LinkedIn is the primary trusted source, USPTO is secondary.
        # Prospeo is NEVER used to determine who the owner is.
        if r.trademark_owner_first_name == NO_INFO and li["owner_first"] != NO_INFO:
            r.trademark_owner_first_name = li["owner_first"]
            r.trademark_owner_last_name  = li["owner_last"]
            r.notes = (r.notes + "; owner name via LinkedIn").lstrip("; ")

        # 6. Prospeo — email only, for the owner we already identified
        print(f"  STEP 6: Prospeo — email lookup")
        delay(1, 2)
        r.owner_email_prospeo = prospeo_get_email(
            r.trademark_owner_first_name,
            r.trademark_owner_last_name,
            r.owner_linkedin_url,
            brand, biz,
        )

        results.append(r)
        save_results(results, output_file)
        print(f"\n  ✓ Saved after brand {i}")

        if i < len(brands):
            delay(3, 6)

    return results


def print_summary(results: list):
    print(f"\n{'='*70}\nRESULTS SUMMARY\n{'='*70}")
    for r in results:
        print(f"\n  Brand              : {r.input_brand}")
        print(f"  Amazon seller      : {r.amazon_seller_name}")
        print(f"  Seller address     : {r.amazon_seller_address}")
        print(f"  Official website   : {r.official_website}")
        print(f"  Contact email      : {r.contact_email}")
        print(f"  Contact form       : {r.contact_form_url}")
        print(f"  TM owner           : {r.trademark_owner_first_name} {r.trademark_owner_last_name}")
        print(f"  Company LinkedIn   : {r.company_linkedin_url}")
        print(f"  Owner LinkedIn     : {r.owner_linkedin_url} ({r.owner_linkedin_title})")
        print(f"  Owner email        : {r.owner_email_prospeo}")
        if r.notes:
            print(f"  Notes              : {r.notes}")


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
        print('No brands. Example:  python3 brand_finder.py "Americanflat"')
        sys.exit(1)

    print(f"Processing {len(brands)} brand(s)...")
    results = process_brands(brands, output_file=args.output)
    print_summary(results)
    csv_file = args.output.replace(".json", ".csv")
    print(f"\nSaved to    : {args.output}")
    print(f"Spreadsheet : {csv_file}  ← open in Excel or Numbers")


if __name__ == "__main__":
    main()
