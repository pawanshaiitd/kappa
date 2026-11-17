# Tool: `submit_patch`

## Signature
`submit_patch() -> str`

## Mechanics & Guardrails
- **Function**: Stages untracked files (`git add -N .`), extracts binary git diff against `_swegemma_baseline` (or `HEAD`), stores the patch, and flags `patch_submitted = True`.
- **Budget Impact**: Consumes **0 tool calls** (`count_tool_call=False`). Can be invoked even when remaining tool calls are 0.
- **Termination Effect**: The harness immediately concludes the agent loop upon completion of the turn.
- **Success Response**: `{"status": "ok", "patch_size": <bytes>, "files_changed": <count>}`.
