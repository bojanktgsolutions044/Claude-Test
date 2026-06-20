"""Tests for the network-coupled functions in brand_finder.

These mock the external boundary -- `requests`, the `DDGS` search client, and
the Anthropic client -- and assert control flow: status handling, exception
safety (graceful `None`/`""`/`[]` returns), fallback ordering, and parsing of
fetched content. No real network calls are made.

`delay` is stubbed out (autouse) so the random sleeps in the production code do
not slow the suite down.
"""
import pytest

import brand_finder as bf
from brand_finder import NO_INFO


# ── Test doubles ──────────────────────────────────────────────────────────────
class FakeResp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, text="", status_code=200, json_data=None):
        self.text = text
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json


class FakeDDGS:
    """Context-manager double for the DDGS search client."""

    results = []
    raise_on_text = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def text(self, query, max_results=8):
        if FakeDDGS.raise_on_text:
            raise RuntimeError("ddg boom")
        return list(FakeDDGS.results)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Never actually sleep during tests.
    monkeypatch.setattr(bf, "delay", lambda *a, **k: None)


# ── safe_get ──────────────────────────────────────────────────────────────────
class TestSafeGet:
    def test_returns_response_on_200(self, monkeypatch):
        resp = FakeResp(text="ok", status_code=200)
        monkeypatch.setattr(bf.requests, "get", lambda *a, **k: resp)
        assert bf.safe_get("https://x.com") is resp

    def test_returns_none_on_non_200(self, monkeypatch):
        monkeypatch.setattr(bf.requests, "get", lambda *a, **k: FakeResp(status_code=404))
        assert bf.safe_get("https://x.com") is None

    def test_returns_none_on_exception(self, monkeypatch):
        def boom(*a, **k):
            raise ConnectionError("down")

        monkeypatch.setattr(bf.requests, "get", boom)
        assert bf.safe_get("https://x.com") is None


# ── ddg ───────────────────────────────────────────────────────────────────────
class TestDdg:
    def test_returns_results_on_success(self, monkeypatch):
        FakeDDGS.results = [{"href": "https://a.com"}, {"href": "https://b.com"}]
        FakeDDGS.raise_on_text = False
        monkeypatch.setattr(bf, "DDGS", FakeDDGS)
        out = bf.ddg("query", n=2)
        assert out == [{"href": "https://a.com"}, {"href": "https://b.com"}]

    def test_returns_empty_list_on_exception(self, monkeypatch):
        FakeDDGS.raise_on_text = True
        monkeypatch.setattr(bf, "DDGS", FakeDDGS)
        assert bf.ddg("query") == []
        FakeDDGS.raise_on_text = False


# ── claude_ask ────────────────────────────────────────────────────────────────
class TestClaudeAsk:
    def test_returns_empty_when_claude_unavailable(self, monkeypatch):
        monkeypatch.setattr(bf, "HAS_CLAUDE", False)
        assert bf.claude_ask("hi") == ""

    def test_returns_stripped_text_on_success(self, monkeypatch):
        class FakeBlock:
            text = "  answer  "

        class FakeMessages:
            def create(self, **kwargs):
                class R:
                    content = [FakeBlock()]

                return R()

        class FakeClient:
            messages = FakeMessages()

        class FakeAnthropic:
            Anthropic = staticmethod(lambda *a, **k: FakeClient())

        monkeypatch.setattr(bf, "HAS_CLAUDE", True)
        monkeypatch.setattr(bf, "anthropic", FakeAnthropic)
        assert bf.claude_ask("hi") == "answer"

    def test_returns_empty_on_exception(self, monkeypatch):
        class FakeAnthropic:
            @staticmethod
            def Anthropic(*a, **k):
                raise RuntimeError("api down")

        monkeypatch.setattr(bf, "HAS_CLAUDE", True)
        monkeypatch.setattr(bf, "anthropic", FakeAnthropic)
        assert bf.claude_ask("hi") == ""


# ── _prospeo_post ─────────────────────────────────────────────────────────────
class TestProspeoPost:
    def test_returns_data_on_success(self, monkeypatch):
        data = {"response": {"people": []}}
        monkeypatch.setattr(
            bf.requests, "post",
            lambda *a, **k: FakeResp(status_code=200, json_data=data),
        )
        assert bf._prospeo_post("/search-person", {}) == data

    def test_returns_none_when_body_has_error(self, monkeypatch):
        data = {"error": True, "error_toast": "rate limited"}
        monkeypatch.setattr(
            bf.requests, "post",
            lambda *a, **k: FakeResp(status_code=200, json_data=data),
        )
        assert bf._prospeo_post("/search-person", {}) is None

    def test_returns_none_on_non_200(self, monkeypatch):
        monkeypatch.setattr(
            bf.requests, "post",
            lambda *a, **k: FakeResp(status_code=500, json_data={}),
        )
        assert bf._prospeo_post("/search-person", {}) is None

    def test_returns_none_on_exception(self, monkeypatch):
        def boom(*a, **k):
            raise TimeoutError("slow")

        monkeypatch.setattr(bf.requests, "post", boom)
        assert bf._prospeo_post("/search-person", {}) is None


# ── find_official_website (control flow over mocked _page_mentions / ddg) ──────
class TestFindOfficialWebsite:
    def test_strategy_a_direct_domain_hit(self, monkeypatch):
        # First direct domain guess verifies -> returned without any search.
        monkeypatch.setattr(bf, "_page_mentions", lambda url, terms: True)
        monkeypatch.setattr(bf, "ddg", lambda *a, **k: pytest.fail("ddg should not run"))
        assert bf.find_official_website("Acme", NO_INFO) == "https://www.acme.com"

    def test_no_candidates_returns_no_info(self, monkeypatch):
        monkeypatch.setattr(bf, "_page_mentions", lambda url, terms: False)
        monkeypatch.setattr(bf, "ddg", lambda *a, **k: [])
        assert bf.find_official_website("Acme", NO_INFO) == NO_INFO

    def test_strategy_b_verified_search_result(self, monkeypatch):
        # Direct guesses fail; a DDG candidate whose page mentions the brand wins.
        monkeypatch.setattr(bf, "_page_mentions", lambda url, terms: "good" in url)
        monkeypatch.setattr(
            bf, "ddg", lambda *a, **k: [{"href": "https://good-brand.com/path"}],
        )
        assert bf.find_official_website("Acme", NO_INFO) == "https://good-brand.com"

    def test_skip_site_domains_are_filtered(self, monkeypatch):
        # Only a skip-listed domain comes back -> nothing verifiable -> NO_INFO.
        monkeypatch.setattr(bf, "_page_mentions", lambda url, terms: False)
        monkeypatch.setattr(
            bf, "ddg", lambda *a, **k: [{"href": "https://www.amazon.com/acme"}],
        )
        assert bf.find_official_website("Acme", NO_INFO) == NO_INFO


# ── find_contact_info ─────────────────────────────────────────────────────────
class TestFindContactInfo:
    def test_no_info_website_short_circuits(self):
        assert bf.find_contact_info(NO_INFO) == (NO_INFO, NO_INFO)

    def test_extracts_email_and_skips_image_filenames(self, monkeypatch):
        html = "contact spam@logo.png or real@acme.com today"
        monkeypatch.setattr(bf, "safe_get", lambda url, *a, **k: FakeResp(text=html))
        email, form = bf.find_contact_info("https://acme.com")
        assert email == "real@acme.com"
        assert form == NO_INFO

    def test_falls_back_to_contact_form(self, monkeypatch):
        html = "<html><body><form action='/send'></form></body></html>"
        monkeypatch.setattr(bf, "safe_get", lambda url, *a, **k: FakeResp(text=html))
        email, form = bf.find_contact_info("https://acme.com")
        assert email == NO_INFO
        assert form == "https://acme.com/contact"

    def test_returns_no_info_when_nothing_fetches(self, monkeypatch):
        monkeypatch.setattr(bf, "safe_get", lambda url, *a, **k: None)
        assert bf.find_contact_info("https://acme.com") == (NO_INFO, NO_INFO)


# ── website_find_founder ──────────────────────────────────────────────────────
class TestWebsiteFindFounder:
    def test_no_info_website_short_circuits(self):
        assert bf.website_find_founder(NO_INFO) == (NO_INFO, NO_INFO)

    def test_extracts_founder_from_about_copy(self, monkeypatch):
        html = "<p>Americanflat was founded in 2017 by Amy Lipton in California.</p>"
        monkeypatch.setattr(bf, "safe_get", lambda url, *a, **k: FakeResp(text=html))
        assert bf.website_find_founder("https://acme.com") == ("Amy", "Lipton")

    def test_returns_no_info_when_no_founder_statement(self, monkeypatch):
        html = "<p>We sell great wall art for your home.</p>"
        monkeypatch.setattr(bf, "safe_get", lambda url, *a, **k: FakeResp(text=html))
        assert bf.website_find_founder("https://acme.com") == (NO_INFO, NO_INFO)
