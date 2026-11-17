# Patch Hygiene & Verification

## 1. Pre-Submission Checklist
Before calling `submit_patch`, ensure:
1. All changes are confined to source implementation files.
2. The targeted test has been run via `run_command` and passes with exit code 0.
3. No temporary files, debug prints, or scratch test scripts were left in the workspace.
4. Call `get_status` to verify budget consumption and remaining turns.

## 2. Calling submit_patch
- `submit_patch` consumes **0 tool-call budget**.
- Verify the returned response has `patch_size > 0` and `files_changed > 0`.
- Once `submit_patch` succeeds, emit a concise final summary of the fix to conclude the session immediately.
