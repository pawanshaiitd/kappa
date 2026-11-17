# Test Isolation & Anti-Patterns

## 1. Absolute Prohibition: Never Modify Test Files
- **NEVER** edit, create, or delete test files (`test_*.py`, `*_test.py`, `tests/`, `conftest.py`, `pytest.ini`, `pyproject.toml`).
- The evaluation harness forcefully strips and resets all test modifications in Phase 2 verification. Any diff touching tests results in automatic evaluation failure.
- Fix the problem exclusively in source implementation files (e.g., `src/`, `fastapi/`, `rich/`, `requests/`, `docs_src/`).

## 2. Never Run Bare Pytest or Full-Repo Sweeps
- **NEVER** run bare `pytest`, `pytest .`, or `python -m unittest discover`.
- Full-repo sweeps take several minutes, exhaust turn budgets, and fail due to pre-existing unrelated breakages.
- ALWAYS specify the exact test file path and test filter:
  - `pytest tests/test_target.py -k test_feature -v`
  - `python -m unittest tests.test_target.TestClass.test_method`

## 3. Handling Pre-Existing Failures
- If an existing test in the suite fails due to missing environment fixtures (e.g. `/test_data`, network mocks), IGNORE IT.
- Do not spend turns attempting to repair unrelated test fixtures or mock infrastructure. Focus solely on verifying your specific fix.
