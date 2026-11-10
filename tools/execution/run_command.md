# Tool: `run_command`

## Signature
`run_command(command: str) -> str`

## Mechanics & Guardrails
- **Execution Target**: `/bin/bash -c <command>` inside `/workspace`.
- **Budget Impact**: Consumes 1 tool call (`@budget_gated`).
- **Timeout**: Enforces a strict 300-second timeout. If exceeded, returns `error_type: "TimeoutExceeded"` without ending the session.
- **Output Truncation**: Standard output and standard error are truncated to 5,000 characters.
- **Exit Code**:
  - `0`: Returns `{"status": "ok", "stdout": "...", "stderr": "...", "exit_code": 0}`.
  - Non-zero: Returns `{"status": "error", "error_type": "CommandError", "details": {"exit_code": N, ...}}`.
