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

## Not tests

`scripts/probe_prospeo.py` is a manual API probe script (live network, no
assertions), deliberately kept out of `tests/` so pytest does not collect it.

## Known gaps (next layers)

The network-coupled functions (`safe_get`, `ddg`, `claude_ask`, `_prospeo_post`
and the stage orchestrators) are not yet covered. Testing them means mocking the
`requests` / DDG / Anthropic boundary and asserting control flow (fallback
ordering, graceful handling of `None` responses). That is the recommended next
milestone.
