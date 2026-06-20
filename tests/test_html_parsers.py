"""Unit tests for the Amazon HTML parsers in brand_finder.

These functions are pure given an HTML string but parse the most fragile part of
the pipeline (Amazon's markup, which changes frequently). The fixtures below are
small, synthetic snippets that mimic the structures each selector targets, so a
regression in a selector/regex is caught without any network access.
"""
import brand_finder as bf


# ── _parse_brand_byline ───────────────────────────────────────────────────────
class TestParseBrandByline:
    def test_visit_the_store_pattern(self):
        html = '<a id="bylineInfo">Visit the Americanflat Store</a>'
        assert bf._parse_brand_byline(html) == "Americanflat"

    def test_brand_colon_pattern(self):
        html = '<a id="bylineInfo">Brand: Acme</a>'
        assert bf._parse_brand_byline(html) == "Acme"

    def test_plain_byline_text_without_store_word(self):
        html = '<a id="bylineInfo">Americanflat</a>'
        assert bf._parse_brand_byline(html) == "Americanflat"

    def test_detail_table_brand_row_fallback(self):
        html = (
            "<table><tr><th>Brand</th><td><span>NeatBrand</span></td></tr></table>"
        )
        assert bf._parse_brand_byline(html) == "NeatBrand"

    def test_returns_empty_when_no_byline(self):
        assert bf._parse_brand_byline("<div>no brand here</div>") == ""


# ── _parse_sold_by ────────────────────────────────────────────────────────────
class TestParseSoldBy:
    def test_seller_profile_trigger_selector(self):
        html = (
            '<a id="sellerProfileTriggerId" '
            'href="/sp?seller=A123">Cool Seller LLC</a>'
        )
        name, url = bf._parse_sold_by(html)
        assert name == "Cool Seller LLC"
        assert url == "https://www.amazon.com/sp?seller=A123"

    def test_amazon_seller_is_ignored_by_primary_selector(self):
        # "Amazon" sellers must be skipped; falls through to "" when nothing else.
        html = (
            '<a id="sellerProfileTriggerId" href="/x">Amazon.com</a>'
        )
        name, url = bf._parse_sold_by(html)
        assert name == ""
        assert url == ""

    def test_merchant_info_buybox_selector(self):
        html = (
            '<div id="tabular-buybox">'
            '<a href="/sp?seller=B999">Buybox Seller</a></div>'
        )
        name, url = bf._parse_sold_by(html)
        assert name == "Buybox Seller"
        assert url == "https://www.amazon.com/sp?seller=B999"

    def test_json_blob_regex_sweep(self):
        html = '{"sellerName":"JSON Seller","sellerURL":"https://x.com/s\\u0026t=1"}'
        name, url = bf._parse_sold_by(html)
        assert name == "JSON Seller"
        assert url == "https://x.com/s&t=1"  # & decoded to &

    def test_sold_by_inline_anchor_regex(self):
        html = 'Sold by: <a href="/foo">Inline Seller</a>'
        name, url = bf._parse_sold_by(html)
        assert name == "Inline Seller"
        assert url == ""

    def test_returns_empty_pair_when_nothing_matches(self):
        assert bf._parse_sold_by("<div>nothing</div>") == ("", "")


# ── _parse_seller_page ────────────────────────────────────────────────────────
class TestParseSellerPage:
    def test_extracts_business_name(self):
        html = "<div>Business Name: Acme Trading Co</div>"
        name, addr = bf._parse_seller_page(html)
        assert name == "Acme Trading Co"

    def test_extracts_multiline_business_address(self):
        html = (
            "<div>Business Address:\n"
            "123 Main St\n"
            "Springfield, IL\n"
            "90210\n"
            "United States\n</div>"
        )
        name, addr = bf._parse_seller_page(html)
        assert addr == "123 Main St, Springfield, IL, 90210, United States"

    def test_returns_empty_when_no_business_info(self):
        name, addr = bf._parse_seller_page("<div>just a product page</div>")
        assert name == ""
        assert addr == ""
