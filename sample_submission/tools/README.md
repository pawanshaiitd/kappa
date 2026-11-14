# Swegemma & ADK Submission Tools Reference

This directory contains the operational contracts, execution parameters, and schemas for the 9 built-in sandboxed tools available in the harness.

## Tool Categories
1. **Execution Tools (`tools/execution/`)**:
   - `run_command`: Subprocess shell execution inside `/workspace` with timeout and output limits.
   - `submit_patch`: Zero-cost diff extraction and task completion marker.
   - `get_status`: Live budget, turn, and patch status inspection.

2. **Workspace File Tools (`tools/workspace/`)**:
   - `read_file`: Line-bounded and character-capped 1-indexed file reader.
   - `edit_file`: 3-tier resilient matcher (`exact`, `flexible`, `regex`) for surgical edits.
   - `write_file`: File creator with automatic directory creation.

3. **Code Intelligence Tools (`tools/graph/`)**:
   - `get_code_neighbors`: Caller/callee graph lookups on pre-built NetworkX AST.
   - `search_similar_code`: 256-d embedding cosine similarity search for symbol resolution.
   - `get_code_subgraph`: Induced subgraph retrieval connecting related symbols.
