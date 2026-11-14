"""Mature Harness Builder for gemma-4-developer-agent / sample_submission.

Architectural references:
- building_a_coding_harness_around_ai.md
- AGENT_PLAN.md
- HARNESS_README.md
"""

from pathlib import Path
import json

SUB_DIR = Path("/Users/pawankumar.s/Documents/gemma-4-developer-agent/sample_submission")
MODEL_NAME = "gemma-4-31b-it-qat-w4a16-ct"


def write(rel_path: str, content: str):
    p = SUB_DIR / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content.strip() + "\n", encoding="utf-8")
    print(f"  [+] Wrote {rel_path}")


def build_rules():
    print("Writing rules/...")
    write(
        "rules/python_standards.md",
        """# Python Engineering Standards

## 1. Syntax & Compatibility
- Target Python 3.10+ syntax while preserving backward compatibility with the repository baseline.
- Never introduce dependencies that are not pre-installed in the hermetic environment.
- Preserve existing code conventions, variable casing, indentation (4 spaces), and docstring styles.

## 2. Type Hints & Signatures
- Use standard typing annotations (`typing.Optional`, `typing.Union`, `typing.List`, `typing.Dict`, `typing.Any` or PEP 604 `|` where already established).
- Do not modify existing function or method public signatures unless explicitly dictated by the problem statement.
- Maintain runtime type safety; avoid unsafe casts or unchecked attribute access.

## 3. Error Handling & Exceptions
- Adhere strictly to the repository's custom exception hierarchy (e.g., `fastapi.exceptions.HTTPException`, `requests.exceptions.RequestException`).
- Never suppress exceptions silently with empty `except:` or `except Exception: pass` blocks.
- Wrap low-level socket or system errors into library-level domain exceptions.

## 4. Minimal Disruption Principle
- Implement only the surgical logic needed to fix the reported bug or satisfy the new behavior.
- Avoid large-scale refactoring, reordering of imports, or stylistic changes in unrelated functions.
""",
    )

    write(
        "rules/test_isolation.md",
        """# Test Isolation & Anti-Patterns

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
""",
    )

    write(
        "rules/file_editing.md",
        """# Precision File Editing Rules

## 1. 3-Tier Matcher Mechanics
The harness employs a 3-tier matching engine for `edit_file`:
- **Tier 1 (Exact)**: Substring match character-by-character.
- **Tier 2 (Flexible)**: Strips leading/trailing whitespace per line, matching structure and auto-reindenting `new_string`.
- **Tier 3 (Regex)**: Splits around code delimiters with flexible whitespace matching.

## 2. Unambiguous Context Selection
- Always provide **3–5 lines of unique surrounding context** in `old_string` above and below the change.
- Never provide a single line (like `return False` or `pass`) as `old_string`, which appears multiple times in the file and triggers a fatal `FileEditError: Multiple matches found`.
- Always verify the file contents with `read_file` before calling `edit_file`.

## 3. Minimal Diff Principle
- Change only the lines required to implement the fix.
- Do not reformat adjacent lines or alter trailing whitespace.
""",
    )

    write(
        "rules/patch_hygiene.md",
        """# Patch Hygiene & Verification

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
""",
    )


def build_tools():
    print("Writing tools/...")
    write(
        "tools/README.md",
        """# Swegemma & ADK Submission Tools Reference

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
""",
    )

    write(
        "tools/execution/run_command.md",
        """# Tool: `run_command`

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
""",
    )

    write(
        "tools/execution/submit_patch.md",
        """# Tool: `submit_patch`

## Signature
`submit_patch() -> str`

## Mechanics & Guardrails
- **Function**: Stages untracked files (`git add -N .`), extracts binary git diff against `_swegemma_baseline` (or `HEAD`), stores the patch, and flags `patch_submitted = True`.
- **Budget Impact**: Consumes **0 tool calls** (`count_tool_call=False`). Can be invoked even when remaining tool calls are 0.
- **Termination Effect**: The harness immediately concludes the agent loop upon completion of the turn.
- **Success Response**: `{"status": "ok", "patch_size": <bytes>, "files_changed": <count>}`.
""",
    )

    write(
        "tools/workspace/read_file.md",
        """# Tool: `read_file`

## Signature
`read_file(filepath: str, start_line: int | None = None, end_line: int | None = None) -> str`

## Mechanics & Guardrails
- **Line Slicing**: 1-indexed inclusive range `[start_line, end_line]`.
- **Caps**: Capped at **150 lines** AND **10,000 characters** per call.
- **Path Resolution**: Relative to `/workspace`. Strips leading `/` or `/workspace/`. Path traversal (`..`) is strictly blocked.
- **Response**: `{"status": "ok", "filepath": "...", "content": "...", "start_line": N, "end_line": M, "total_lines": T, "is_truncated": bool}`.
""",
    )

    write(
        "tools/workspace/edit_file.md",
        """# Tool: `edit_file`

## Signature
`edit_file(filepath: str, old_string: str, new_string: str, allow_multiple: bool = False) -> str`

## 3-Tier Matcher Algorithm
1. **Tier 1 (Exact)**: Direct substring matching.
2. **Tier 2 (Flexible)**: Strips leading/trailing whitespace per line, matches code structure, and re-indents `new_string` to the baseline indentation of the match.
3. **Tier 3 (Regex)**: Tokenizes `old_string` around code delimiters and allows flexible whitespace.

## Uniqueness Requirement
If `old_string` matches more than 1 location and `allow_multiple=False`, the tool aborts with `FileEditError: Multiple matches found`. Provide 3+ lines of unique context to guarantee single-site targeting.
""",
    )

    write(
        "tools/workspace/write_file.md",
        """# Tool: `write_file`

## Signature
`write_file(filepath: str, content: str) -> str`

## Mechanics
- Writes full file content to `/workspace/<filepath>`.
- Automatically creates intermediate parent directories (`mkdir -p`).
- Response: `{"status": "ok", "filepath": "...", "size": <bytes>}`.
""",
    )

    write(
        "tools/graph/code_intelligence.md",
        """# Tools: Code Intelligence Graph Suite

## 1. `search_similar_code(query: str, k: int = 5) -> str`
- Computes cosine similarity between precomputed 256-d symbol embeddings and query tokens.
- Returns top-k matching symbols (classes, functions, methods) with dotted paths.

## 2. `get_code_neighbors(node: str, edge_type: str | None = None, max_neighbors: int = 50) -> str`
- Queries the NetworkX AST dependency graph for incoming and outgoing caller/callee relationships.
- Edge types: `calls`, `imports`, `inherits`.

## 3. `get_code_subgraph(nodes: list[str]) -> str`
- Extracts the induced call subgraph connecting the specified list of symbol nodes.
""",
    )

    schemas = {
        "run_command": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Shell command to execute in /workspace"}},
            "required": ["command"],
        },
        "read_file": {
            "type": "object",
            "properties": {
                "filepath": {"type": "string", "description": "Path relative to /workspace"},
                "start_line": {"type": "integer", "description": "1-based start line"},
                "end_line": {"type": "integer", "description": "1-based end line"},
            },
            "required": ["filepath"],
        },
        "edit_file": {
            "type": "object",
            "properties": {
                "filepath": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "allow_multiple": {"type": "boolean", "default": False},
            },
            "required": ["filepath", "old_string", "new_string"],
        },
        "write_file": {
            "type": "object",
            "properties": {"filepath": {"type": "string"}, "content": {"type": "string"}},
            "required": ["filepath", "content"],
        },
        "submit_patch": {"type": "object", "properties": {}},
        "get_status": {"type": "object", "properties": {}},
        "search_similar_code": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "k": {"type": "integer", "default": 5}},
            "required": ["query"],
        },
        "get_code_neighbors": {
            "type": "object",
            "properties": {
                "node": {"type": "string"},
                "edge_type": {"type": "string"},
                "max_neighbors": {"type": "integer", "default": 50},
            },
            "required": ["node"],
        },
        "get_code_subgraph": {
            "type": "object",
            "properties": {"nodes": {"type": "array", "items": {"type": "string"}}},
            "required": ["nodes"],
        },
    }
    write("tools/tool_schemas.json", json.dumps(schemas, indent=2))


def build_memory():
    print("Writing memory/...")
    write(
        "memory/README.md",
        """# Multi-Tier Agent Memory Architecture

This directory defines the memory management contracts for the agent harness, following Section 8 of `building_a_coding_harness_around_ai.md`:
1. **Working / Session Memory (`session_state.md`)**: In-flight reasoning state, active goal, tracked files, and problem description.
2. **Episodic Memory & Trajectory Distillation (`trajectory_cache.json`)**: Lessons distilled from previous task attempts, anti-pattern detections, and optimal recovery paths.
3. **Structured Facts & Memory Schema (`memory_schema.json`)**: Typed schema for project-specific constraints, dependency mappings, and API contracts.
""",
    )

    write(
        "memory/session_state.md",
        """# Session State Architecture

ADK sessions persist key-value variables across multi-turn executions in `session.state`:

## Managed State Variables
- `problem_description` (`str`): Task issue statement provided by benchmark harness.
- `hints` (`str | None`): Community or author hints for target issue.
- `active_phase` (`"PLANNING" | "EXECUTION" | "VERIFICATION"`): State-machine gate to prevent premature edits.
- `target_files` (`list[str]`): Discovered files requiring surgical modifications.
- `edited_files` (`list[str]`): Tracking files modified during the session.
- `last_test_command` (`str`): The specific targeted pytest command used for validation.
- `patch_submitted` (`bool`): Flag set to true upon successful `submit_patch` invocation.
""",
    )

    trajectory_cache = {
        "version": "1.0",
        "distilled_lessons": [
            {
                "pattern_id": "P01_NON_UNIQUE_OLD_STRING",
                "trigger": "FileEditError: Multiple matches found",
                "remediation": "Re-read file with read_file for 5 lines of surrounding context, include function def or enclosing block in old_string.",
            },
            {
                "pattern_id": "P02_BARE_PYTEST_TIMEOUT",
                "trigger": "TimeoutExceeded on pytest",
                "remediation": "Never run pytest without explicit file path. Locate test via find tests -name '*test_*.py' and run single file.",
            },
            {
                "pattern_id": "P03_PROTECTED_TEST_TOUCH",
                "trigger": "Edit in tests/ or test_*.py",
                "remediation": "Immediately revert changes to test files. Solve root cause in source implementation under src/ or library package.",
            },
            {
                "pattern_id": "P04_EMPTY_PATCH_SUBMIT",
                "trigger": "submit_patch returns patch_size: 0",
                "remediation": "Do not submit patch without modifying files. Verify git diff before calling submit_patch.",
            },
        ],
    }
    write("memory/trajectory_cache.json", json.dumps(trajectory_cache, indent=2))

    memory_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "AgentMemorySchema",
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "task_repo": {"type": "string"},
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "phase": {"type": "string", "enum": ["PLANNING", "EXECUTION", "VERIFICATION"]},
                        "finding": {"type": "string"},
                        "target_file": {"type": "string"},
                        "action": {"type": "string"},
                    },
                    "required": ["phase", "action"],
                },
            },
        },
        "required": ["session_id", "task_repo", "decisions"],
    }
    write("memory/memory_schema.json", json.dumps(memory_schema, indent=2))


def build_asr():
    print("Writing asr/...")
    write(
        "asr/README.md",
        """# Automated Speech Recognition (ASR) & Multimodal Audio Architecture

This directory specifies the multimodal audio and speech processing pipeline for the agent harness:
1. **Audio Ingestion & Preprocessing (`audio_spec.md`)**: Supported formats, sample rates, voice activity detection (VAD), and chunking.
2. **Transcription & Intent Pipeline (`transcription_pipeline.md`)**: Speech-to-text decoding, code symbol normalization, and direct intent mapping to tool calls.
3. **Structured Schema (`audio_schema.json`)**: Data interchange format for transcribed developer audio and voice directives.
""",
    )

    write(
        "asr/audio_spec.md",
        """# Multimodal Audio Input Specifications

## 1. Supported Audio Formats & Codecs
- **Container Formats**: WAV, MP3, FLAC, OGG, WebM.
- **Sample Rates**: 16,000 Hz (speech recognition standard) or 44,100 / 48,000 Hz.
- **Channels**: Mono preferred; stereo channels are downmixed to single channel prior to acoustic modeling.
- **Bit Depth**: 16-bit linear PCM.

## 2. Audio Token Budgeting & Chunking
- Audio segments are partitioned into bounded frames of 30 seconds maximum.
- Voice Activity Detection (VAD) automatically strips leading and trailing silence (>500ms) to conserve context tokens.
- Streaming audio chunks are processed incrementally with speculative beam search.
""",
    )

    write(
        "asr/transcription_pipeline.md",
        """# Speech-to-Intent Transcription Pipeline

```
[Raw Developer Audio]
        |
        v
[Voice Activity Detection (VAD)] -> (Strip Silence)
        |
        v
[Acoustic Decoder (Whisper / Conformer / Gemma-Audio)]
        |
        v
[Code Symbol Post-Processing]
  - Normalizes spoken terms into code syntax:
    - "dot py" -> ".py"
    - "fast a p i" -> "FastAPI"
    - "snake_case", "camelCase" reconstruction
        |
        v
[Intent Classifier & Tool Routing]
  - Maps natural speech directly to tool calls:
    - "Read module openapi dot py lines 50 to 100" -> read_file(filepath="openapi.py", start_line=50, end_line=100)
    - "Run targeted test test underscore target" -> run_command(command="pytest tests/test_target.py")
```
""",
    )

    audio_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "AudioTranscriptionSchema",
        "type": "object",
        "properties": {
            "audio_id": {"type": "string"},
            "sample_rate_hz": {"type": "integer", "enum": [16000, 22050, 44100, 48000]},
            "duration_seconds": {"type": "number"},
            "transcription": {"type": "string"},
            "confidence_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "detected_intent": {"type": "string"},
            "mapped_tool_call": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["tool_name", "arguments"],
            },
        },
        "required": ["audio_id", "transcription", "confidence_score"],
    }
    write("asr/audio_schema.json", json.dumps(audio_schema, indent=2))


def build_skills():
    print("Writing skills/...")
    # 1. repo_navigation
    write(
        "skills/repo_navigation/SKILL.md",
        """---
name: repo_navigation
description: High-speed repository navigation, locating target files and symbols without broad sweeps.
---

# Repository Navigation Skill

## Objective
Quickly identify the exact source files and line ranges responsible for the reported issue in under 2 turns.

## Procedure
1. Inspect the problem statement for explicit module names, function names, classes, or CLI flags.
2. If explicit paths are provided, call `read_file` immediately on the target range.
3. If only error messages or symbols are mentioned, query `search_similar_code(query="symbol_name")`.
4. Avoid broad `find .` or `grep` sweeps across `/workspace` to conserve context and tool budgets.
""",
    )
    write(
        "skills/repo_navigation/resources/navigation_guide.md",
        """# Repository Directory Patterns
- `fastapi/`: core framework code in `fastapi/`, docs code in `docs_src/`.
- `rich/`: core terminal renderers in `rich/`.
- `requests/`: HTTP client logic in `requests/`.
- `httpx/`: Async HTTP client in `httpx/`.
""",
    )
    write(
        "skills/repo_navigation/scripts/locate_symbol.py",
        """# Symbol Locator Script
import sys

def suggest_search_query(symbol: str) -> str:
    parts = symbol.strip().split(".")
    return parts[-1]

if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(suggest_search_query(sys.argv[1]))
""",
    )

    # 2. code_intelligence
    write(
        "skills/code_intelligence/SKILL.md",
        """---
name: code_intelligence
description: Query AST call graphs and symbol embeddings to trace root cause callers and callees.
---

# Code Intelligence Skill

## Objective
Trace symbol relationships and cross-module dependencies using precomputed AST graphs.

## Procedure
1. Identify the primary function or class associated with the issue.
2. Call `get_code_neighbors(node="pkg.module.Class.method", edge_type="calls")` to inspect callers.
3. Retrieve the minimal induced subgraph via `get_code_subgraph(nodes=[...])`.
4. Formulate the precise root-cause analysis before editing code.
""",
    )
    write(
        "skills/code_intelligence/resources/graph_patterns.md",
        """# Graph Edge Conventions
- `calls`: Function invocation or method dispatch.
- `imports`: Module-level import dependency.
- `inherits`: Class inheritance relationship.
""",
    )
    write(
        "skills/code_intelligence/scripts/trace_callers.py",
        """# AST Caller Tracer Helper
import sys

def format_node_query(node: str) -> str:
    return node.strip().replace("/", ".")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(format_node_query(sys.argv[1]))
""",
    )

    # 3. precision_editing
    write(
        "skills/precision_editing/SKILL.md",
        """---
name: precision_editing
description: Surgical code editing using 3-tier matching engine without whitespace churn.
---

# Precision Editing Skill

## Objective
Apply surgical code modifications to existing source files with zero ambiguity errors.

## Procedure
1. Always call `read_file` on the target line range before formulating the edit.
2. Select **3 to 5 lines of unique surrounding context** above and below the change.
3. Formulate `old_string` containing the exact lines from `read_file`.
4. Formulate `new_string` with the minimal required bug fix or logic update.
5. Call `edit_file(filepath=..., old_string=..., new_string=...)`.
6. Verify the returned `occurrences == 1` and examine the generated `diff`.
""",
    )
    write(
        "skills/precision_editing/resources/editing_examples.md",
        """# Surgical Edit Examples

## Bad (Ambiguous):
```python
old_string = "    return None"
```
*Triggers `FileEditError: Multiple matches found`.*

## Good (Unique Context):
```python
old_string = \"\"\"    def get_token(self, request: Request) -> Optional[str]:
        header = request.headers.get("Authorization")
        if not header:
            return None\"\"\"
```
*Matches exactly 1 occurrence.*
""",
    )
    write(
        "skills/precision_editing/scripts/validate_hunk.py",
        """# Hunk Validator Helper
import sys

def check_hunk_lines(text: str) -> int:
    return len(text.strip().splitlines())

if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(check_hunk_lines(sys.argv[1]))
""",
    )

    # 4. test_verification
    write(
        "skills/test_verification/SKILL.md",
        """---
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
""",
    )
    write(
        "skills/test_verification/resources/pytest_commands.md",
        """# Standard Targeted Test Commands
- Single test method: `pytest tests/test_target.py -k test_method_name -v`
- Single test file: `pytest tests/test_target.py -v`
- Unittest single file: `python -m unittest tests.test_target`
""",
    )
    write(
        "skills/test_verification/scripts/parse_test_result.py",
        """# Test Result Parser
import sys

def parse_exit_code(code: int) -> str:
    return "PASSED" if code == 0 else "FAILED"

if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(parse_exit_code(int(sys.argv[1])))
""",
    )


def build_prompts():
    print("Writing prompts/...")
    write(
        "prompts/system.md",
        """You are the Master Orchestrator and Lead Autonomous Software Engineer for MKPRO SWE engineering.
You operate within a sandboxed engineering harness with strict operational boundaries, two-phase execution, and hermetic verification.

## Core Objective: Fast, Minimal, and Verified Solutions
Aim to understand, resolve, and submit the fix in the minimum number of tool calls (under 8–10 turns). Move decisively from the problem statement to the relevant files, apply surgical modifications, verify with a targeted test, and submit.

## Mandatory Two-Phase Workflow

### Phase 1: Exploration & Planning (Do Not Modify Files)
1. Read the problem statement and identify candidate files, error messages, and affected symbols.
2. Delegate to the `code_analyzer_agent` or call `search_similar_code` / `read_file` to locate the exact lines.
3. Formulate a minimal, concrete implementation plan before touching code.

### Phase 2: Surgical Execution & Targeted Verification
1. Apply minimal modifications to source implementation files using `edit_file` (with 3+ lines of unique surrounding context) or `write_file`.
2. **Targeted Test Execution**: Run ONLY the specific test file or method verifying the bug:
   - `pytest tests/test_target.py -k test_feature -v`
   - **NEVER** run bare `pytest`, `pytest .`, or full-repo sweeps.
   - **NEVER** edit, create, or delete test files (`test_*.py`, `tests/`).
3. **Patch Submission**:
   - Once the targeted test passes with exit code 0, call `submit_patch` immediately.
   - Verify `patch_size > 0` and `files_changed > 0`.
   - Provide a concise final summary of what was changed and verified to end the session.

## Operational Rules & Anti-Patterns
- **NEVER modify test files**: All changes must be in source files under `/workspace`. Modifying tests results in an automatic evaluation failure.
- **NEVER search outside `/workspace`**: All repository and test dependencies are pre-installed. Do not look for system packages in `/usr/local/` or `/wheels/`.
- **NEVER ignore continuation nudges**: If a nudge is received, emit your next tool call immediately without repeating prior reasoning.
- **Preserve budgets**: Conserve tool calls and wall-clock time; call `get_status` if approaching limits.
""",
    )

    write(
        "prompts/analyzer.md",
        """You are the Code Analyzer Specialist sub-agent.
Your sole mission is to trace repository call hierarchies, examine AST symbol graphs, and pinpoint the exact source files and line ranges responsible for the bug.

## Workflow
1. Use `search_similar_code(query="...")` with keywords from the problem statement or error trace.
2. Use `get_code_neighbors(node="...")` to trace callers and callees of target symbols.
3. Use `read_file` with targeted line ranges to inspect candidate implementations.
4. Return a structured analysis report:
   - Target File Path
   - Target Function / Class / Line Range
   - Root Cause Mechanism
   - Recommended Minimal Fix
""",
    )

    write(
        "prompts/planner.md",
        """You are the Architect and Planning Specialist sub-agent.
Your role is to formulate a minimal, dependency-aware implementation and testing plan.

## Responsibilities
1. Evaluate problem statement constraints, API backwards-compatibility, and edge cases.
2. Verify that proposed changes will not disrupt existing public interfaces or signatures.
3. Outline the exact sequence of file modifications and the exact targeted pytest command required to verify the fix.
""",
    )

    write(
        "prompts/editor.md",
        """You are the Precision Code Editor sub-agent.
Your sole responsibility is to apply surgical code replacements using the 3-tier matcher.

## Instructions
1. Always verify the current code state with `read_file` before making any edit.
2. Ensure `old_string` includes 3–5 lines of unique surrounding context to prevent multiple-match ambiguity.
3. Apply minimal necessary changes; never churn whitespace or reformat unrelated methods.
4. Verify the returned diff from `edit_file`.
""",
    )

    write(
        "prompts/verifier.md",
        """You are the Targeted Test Verifier sub-agent.
Your responsibility is to design and execute hermetic targeted test verification commands.

## Instructions
1. Locate the specific test file verifying the affected component.
2. Execute targeted pytest: `pytest <test_file> -k <test_method> -v`.
3. Check exit code and parse assertion error messages.
4. Report whether the implementation satisfies the behavioral contract.
5. **NEVER** edit files under `tests/` or create test stubs.
""",
    )


def build_subagents():
    print("Writing sub_agents/...")
    write(
        "sub_agents/code_analyzer.yaml",
        f"""name: code_analyzer_agent
description: Analyzes repository source files and symbol graphs to locate root causes.
model: {MODEL_NAME}
adapter: tool_lora
instruction: !include ../prompts/analyzer.md
tools:
  - read_file
  - search_similar_code
  - get_code_neighbors
  - get_code_subgraph
generate_content_config: !include ../configs/sampling.yaml
""",
    )

    write(
        "sub_agents/architect_planner.yaml",
        f"""name: architect_planner_agent
description: Plans surgical modifications, checks architectural contracts, and selects targeted tests.
model: {MODEL_NAME}
instruction: !include ../prompts/planner.md
tools:
  - read_file
  - search_similar_code
  - get_code_neighbors
  - get_status
generate_content_config: !include ../configs/sampling.yaml
""",
    )

    write(
        "sub_agents/precision_editor.yaml",
        f"""name: precision_editor_agent
description: Applies surgical, unambiguous code edits to source files using 3-tier matching.
model: {MODEL_NAME}
adapter: main_lora
instruction: !include ../prompts/editor.md
tools:
  - read_file
  - edit_file
  - write_file
generate_content_config: !include ../configs/sampling.yaml
""",
    )

    write(
        "sub_agents/targeted_verifier.yaml",
        f"""name: targeted_verifier_agent
description: Executes targeted tests and validates patch correctness without full-repo sweeps.
model: {MODEL_NAME}
instruction: !include ../prompts/verifier.md
tools:
  - run_command
  - read_file
  - get_status
generate_content_config: !include ../configs/sampling.yaml
""",
    )


def build_configs_and_roots():
    print("Writing configs/, eval_config.yaml, agent.yaml...")
    write(
        "configs/sampling.yaml",
        """temperature: 0.1
top_p: 0.95
max_output_tokens: 2048
stop:
  - "<|endoftext|>"
  - "<|im_end|>"
""",
    )

    write(
        "eval_config.yaml",
        """# Optimized participant evaluation configuration for swegemma SWE-bench harness.
# Controls per-task execution budgets, command timeouts, and loop limits.
evaluation:
  timeout_seconds: 300
  max_tool_calls: 35
  max_time_minutes: 30
  max_turns: 50
""",
    )

    write(
        "agent.yaml",
        f"""name: mkpro_coordinator
description: Master orchestrator and multi-agent coordinator for MKPRO SWE engineering.
model: {MODEL_NAME}
adapter: main_lora
instruction: !include prompts/system.md
tools:
  - run_command
  - read_file
  - edit_file
  - write_file
  - get_status
  - submit_patch
  - get_code_neighbors
  - search_similar_code
  - get_code_subgraph
  - agent_tool:
      config_path: sub_agents/code_analyzer.yaml
      skip_summarization: true
  - agent_tool:
      config_path: sub_agents/architect_planner.yaml
      skip_summarization: true
  - agent_tool:
      config_path: sub_agents/precision_editor.yaml
      skip_summarization: true
  - agent_tool:
      config_path: sub_agents/targeted_verifier.yaml
      skip_summarization: true
skills:
  - skills/repo_navigation
  - skills/code_intelligence
  - skills/precision_editing
  - skills/test_verification
generate_content_config: !include configs/sampling.yaml
""",
    )


def main():
    print("Starting Mature Harness Generation in sample_submission...")
    build_rules()
    build_tools()
    build_memory()
    build_asr()
    build_skills()
    build_prompts()
    build_subagents()
    build_configs_and_roots()
    print("All mature harness assets generated successfully!")


if __name__ == "__main__":
    main()
