# Tool: `read_file`

## Signature
`read_file(filepath: str, start_line: int | None = None, end_line: int | None = None) -> str`

## Mechanics & Guardrails
- **Line Slicing**: 1-indexed inclusive range `[start_line, end_line]`.
- **Caps**: Capped at **150 lines** AND **10,000 characters** per call.
- **Path Resolution**: Relative to `/workspace`. Strips leading `/` or `/workspace/`. Path traversal (`..`) is strictly blocked.
- **Response**: `{"status": "ok", "filepath": "...", "content": "...", "start_line": N, "end_line": M, "total_lines": T, "is_truncated": bool}`.
