"""Unit tests for the pure, dependency-free logic in brand_finder.

These functions encode the core business rules of the pipeline (address /
country detection, name parsing, slug/URL helpers, payload selection). They are
deterministic and need no network or mocking, which makes them the highest-value
first layer of test coverage.
"""
import brand_finder as bf
from brand_finder import NO_INFO


# ── is_us_address ─────────────────────────────────────────────────────────────
class TestIsUsAddress:
    def test_empty_and_no_info_default_to_us_eligible(self):
        # Unknown addresses must NOT skip USPTO, so they count as US-eligible.
        assert bf.is_us_address("") is True
        assert bf.is_us_address(NO_INFO) is True

    def test_explicit_usa_suffix(self):
        assert bf.is_us_address("123 Main St, Springfield, IL, USA") is True
        assert bf.is_us_address("500 Market St, San Francisco, US") is True

    def test_us_zip_near_end(self):
        assert bf.is_us_address("742 Evergreen Terrace, Springfield, CA 90210") is True
        assert bf.is_us_address("742 Evergreen Terrace, CA 90210-1234") is True

    def test_non_us_country_in_last_segment(self):
        assert bf.is_us_address("Some Rd, Shenzhen, China") is False
        assert bf.is_us_address("221B Baker Street, London, United Kingdom") is False
        assert bf.is_us_address("1 Industrial Park, Shenzhen, CN") is False

    def test_non_latin_characters_imply_non_us(self):
        assert bf.is_us_address("深圳市某某区某街道") is False

    def test_plain_domestic_address_without_country_defaults_us(self):
        # No country marker and no ZIP -> default to US-eligible.
        assert bf.is_us_address("Some Street, Some Town") is True


# ── _detect_country ───────────────────────────────────────────────────────────
class TestDetectCountry:
    def test_empty_returns_blank(self):
        assert bf._detect_country("") == ""
        assert bf._detect_country(NO_INFO) == ""

    def test_cjk_detected_as_cn(self):
        assert bf._detect_country("广东省深圳市") == "CN"

    def test_us_zip_and_suffix(self):
        assert bf._detect_country("Main St, TX 73301") == "US"
        assert bf._detect_country("Main St, Austin, USA") == "US"

    def test_alias_mapping(self):
        assert bf._detect_country("10 Downing St, London, England") == "GB"
        assert bf._detect_country("Tower, Dubai, United Arab Emirates") == "AE"

    def test_unknown_returns_blank(self):
        # A last segment that is too long to be a country code/name.
        assert bf._detect_country("just one long descriptive line with no comma") == ""


# ── _is_offshore ──────────────────────────────────────────────────────────────
class TestIsOffshore:
    def test_china_address_is_offshore(self):
        assert bf._is_offshore("Factory Rd, Shenzhen, China") is True

    def test_us_address_is_not_offshore(self):
        assert bf._is_offshore("Main St, Austin, TX 73301") is False

    def test_any_offshore_among_many_triggers_true(self):
        assert bf._is_offshore("Main St, Austin, USA", "Plant, Hanoi, Vietnam") is True

    def test_all_unknown_is_not_offshore(self):
        assert bf._is_offshore("", NO_INFO) is False


# ── _name_from_linkedin_title ─────────────────────────────────────────────────
class TestNameFromLinkedinTitle:
    def test_standard_dash_separated_title(self):
        first, last = bf._name_from_linkedin_title(
            "Giorgio Piccoli - Founder - Americanflat | LinkedIn"
        )
        assert (first, last) == ("Giorgio", "Piccoli")

    def test_multi_word_last_name(self):
        first, last = bf._name_from_linkedin_title("Maria Del Carmen Ruiz - CEO")
        assert first == "Maria"
        assert last == "Del Carmen Ruiz"

    def test_single_token_is_rejected(self):
        assert bf._name_from_linkedin_title("Founder | LinkedIn") == (NO_INFO, NO_INFO)

    def test_overly_long_head_is_rejected(self):
        long_name = "a " * 30
        assert bf._name_from_linkedin_title(long_name + " - CEO") == (NO_INFO, NO_INFO)

    def test_empty_is_rejected(self):
        assert bf._name_from_linkedin_title("") == (NO_INFO, NO_INFO)


# ── _slug / _similar ──────────────────────────────────────────────────────────
class TestSlugAndSimilar:
    def test_slug_strips_non_alphanumerics_and_lowercases(self):
        assert bf._slug("American Flat!") == "americanflat"
        assert bf._slug("ACME-Co, Inc.") == "acmecoinc"

    def test_similar_when_one_contains_the_other(self):
        assert bf._similar("Americanflat", "American Flat LLC") is True
        assert bf._similar("Acme", "ACME Corporation") is True

    def test_not_similar_for_distinct_names(self):
        assert bf._similar("Nike", "Adidas") is False

    def test_empty_slug_is_not_similar(self):
        assert bf._similar("", "Anything") is False
        assert bf._similar("!!!", "Anything") is False


# ── _domain_from_url / homepage ───────────────────────────────────────────────
class TestUrlHelpers:
    def test_domain_strips_scheme_www_path_and_query(self):
        assert bf._domain_from_url("https://www.Example.com/path?x=1") == "example.com"
        assert bf._domain_from_url("http://example.com") == "example.com"

    def test_homepage_keeps_scheme_and_host_only(self):
        assert bf.homepage("https://example.com/a/b?c=d") == "https://example.com"

    def test_homepage_returns_input_when_no_match(self):
        assert bf.homepage("not-a-url") == "not-a-url"


# ── _candidate_domains ────────────────────────────────────────────────────────
class TestCandidateDomains:
    def test_builds_expected_variants(self):
        out = bf._candidate_domains("Acme")
        assert "https://www.acme.com" in out
        assert "https://acme.com" in out
        assert "https://acme.co" in out

    def test_empty_name_yields_no_candidates(self):
        assert bf._candidate_domains("") == []

    def test_overly_long_slug_yields_no_candidates(self):
        # Slug longer than 30 chars is rejected to avoid junk URLs.
        assert bf._candidate_domains("a" * 31) == []


# ── _registry_url ─────────────────────────────────────────────────────────────
class TestRegistryUrl:
    def test_known_country_uses_specific_registry(self):
        url = bf._registry_url("GB", "Acme Ltd")
        assert "company-information.service.gov.uk" in url
        assert "Acme+Ltd" in url

    def test_unknown_country_uses_default_registry(self):
        url = bf._registry_url("ZZ", "Acme & Co")
        assert url.startswith(bf.DEFAULT_REGISTRY_URL.split("{")[0])
        # quote_plus encodes the ampersand and space.
        assert "Acme+%26+Co" in url

    def test_country_code_is_case_insensitive(self):
        assert bf._registry_url("gb", "X") == bf._registry_url("GB", "X")


# ── _pick_best_person ─────────────────────────────────────────────────────────
class TestPickBestPerson:
    def test_prefers_highest_priority_title(self):
        people = [
            {"person": {"name": "C-level", "current_job_title": "CEO"}},
            {"person": {"name": "The Founder", "current_job_title": "Founder"}},
        ]
        # "Founder" outranks "CEO" in OWNER_TITLE_SEARCHES.
        assert bf._pick_best_person(people)["name"] == "The Founder"

    def test_falls_back_to_first_when_no_title_matches(self):
        people = [
            {"person": {"name": "First", "current_job_title": "Intern"}},
            {"person": {"name": "Second", "current_job_title": "Analyst"}},
        ]
        assert bf._pick_best_person(people)["name"] == "First"

    def test_handles_unwrapped_person_dicts(self):
        people = [{"name": "Flat", "current_job_title": "Owner"}]
        assert bf._pick_best_person(people)["name"] == "Flat"

    def test_empty_list_returns_none(self):
        assert bf._pick_best_person([]) is None

    def test_missing_title_does_not_crash(self):
        people = [{"person": {"name": "NoTitle"}}]
        assert bf._pick_best_person(people)["name"] == "NoTitle"


# ── _extract_email_from_enrich ────────────────────────────────────────────────
class TestExtractEmailFromEnrich:
    def test_none_returns_empty(self):
        assert bf._extract_email_from_enrich(None) == ""

    def test_email_as_dict_with_email_key(self):
        data = {"response": {"email": {"email": "a@b.com"}}}
        assert bf._extract_email_from_enrich(data) == "a@b.com"

    def test_email_as_dict_with_value_key(self):
        data = {"response": {"email": {"value": "c@d.com"}}}
        assert bf._extract_email_from_enrich(data) == "c@d.com"

    def test_email_as_plain_string(self):
        data = {"response": {"email": "e@f.com"}}
        assert bf._extract_email_from_enrich(data) == "e@f.com"

    def test_falls_back_to_emails_list(self):
        data = {"response": {"email": "", "emails": [{"email": "g@h.com"}]}}
        assert bf._extract_email_from_enrich(data) == "g@h.com"

    def test_emails_list_of_plain_strings(self):
        data = {"response": {"emails": ["i@j.com"]}}
        assert bf._extract_email_from_enrich(data) == "i@j.com"

    def test_payload_without_response_wrapper(self):
        # Function falls back to the top-level dict when "response" is absent.
        assert bf._extract_email_from_enrich({"email": "k@l.com"}) == "k@l.com"

    def test_no_email_returns_empty(self):
        assert bf._extract_email_from_enrich({"response": {}}) == ""
