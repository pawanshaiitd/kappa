# Tool: `write_file`

## Signature
`write_file(filepath: str, content: str) -> str`

## Mechanics
- Writes full file content to `/workspace/<filepath>`.
- Automatically creates intermediate parent directories (`mkdir -p`).
- Response: `{"status": "ok", "filepath": "...", "size": <bytes>}`.
