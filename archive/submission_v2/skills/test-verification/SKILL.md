---
name: test-verification
description: Verify a fix with a reproduction script and one targeted pytest run, without full-suite sweeps.
---

# Test Verification

## Objective
Get evidence that the fix works, in under a minute of wall clock.

## Procedure
1. **Reproduce first.** Write a short script to `/tmp` (never `/workspace`) and run it:
   ```
   cat > /tmp/repro.py <<'EOF'
   from pkg import thing
   assert thing(bad_input) == expected, thing(bad_input)
   print("OK")
   EOF
   python /tmp/repro.py
   ```
   Run it before the fix to see it fail, and after to see it pass. This is the fastest signal available.
2. **Then one targeted test file.** Find it with `ls tests/ | grep <area>`, then:
   ```
   python -m pytest tests/test_<area>.py -q -x
   ```
   Add `-k <test_name>` to narrow further. `-x` stops at the first failure and saves time.
3. Read the exit code and the first assertion error. Re-read the code, correct, and retry at most twice.

## Anti-patterns
- `pytest`, `pytest .`, `pytest tests/` — runs thousands of tests and burns the whole time budget.
- Writing a test file into `tests/` — it is stripped before scoring and can only waste a tool call.
- Treating "no output" as success: always check the exit code.

## Note
Pre-existing unrelated failures in a test file are not caused by your change. Confirm by checking whether the
failing test touches the code you modified; if it does not, proceed to submit.
