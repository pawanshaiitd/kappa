---
name: test_verification
description: Execute targeted, hermetic test cases and assert fix correctness without full-repo sweeps.
---

# Test Verification Skill

## Objective
Verify the correctness of the applied fix using targeted pytest executions without running full suites.

## Procedure
1. Identify the specific test file that targets the modified component (e.g., `tests/test_routing.py`).
2. Run only the targeted test method via `run_command(command="pytest tests/test_routing.py -k test_custom_route -v")`.
3. Check `exit_code == 0`.
4. If the test passes, proceed directly to `submit_patch`.
5. If the test fails on your modified code, inspect assertion error and apply bounded correction (max 2 attempts).
6. **NEVER** modify files under `tests/`.
