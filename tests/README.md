# Tests

Unit tests for `brand_finder.py`. They are hermetic — no network calls — so they
run fast and deterministically.

## Running

```bash
pip install -r requirements-dev.txt
pytest
```

## Layout

- `test_pure_logic.py` — address/country detection, name parsing, slug/URL
  helpers, registry URLs, Prospeo payload selection/extraction.
- `test_html_parsers.py` — the Amazon byline / sold-by / seller-page parsers,
  driven by small synthetic HTML fixtures.
- `test_output.py` — `BrandResult` defaults and `save_results` JSON/CSV output,
  including the manual-worklist split.
- `test_network_mocked.py` — the network-coupled functions (`safe_get`, `ddg`,
  `claude_ask`, `_prospeo_post`, `find_official_website`, `find_contact_info`,
  `website_find_founder`) with the `requests` / DDG / Anthropic boundary mocked.
  Asserts status handling, exception safety, fallback ordering, and parsing of
  fetched content. `delay` is stubbed so the suite never sleeps.

## Not tests

`scripts/probe_prospeo.py` is a manual API probe script (live network, no
assertions), deliberately kept out of `tests/` so pytest does not collect it.

## Known gaps (next layers)

The remaining uncovered code is the higher-level pipeline orchestration —
`process_brands`, `stage_d_fallback`, and the USPTO/TSDR/registry stages. These
chain many of the now-tested helpers together; covering them means mocking the
same boundary and asserting the multi-stage fallback logic and `BrandResult`
field population end to end.
