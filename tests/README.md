# Tests

HTTP is mocked with respx;
no test touches a real workspace. Fixtures go in `tests/fixtures/` as sanitized copies of real
API responses (start from `demo/out/`).

Run: `uv run pytest`
