---
name: precision-editing
description: Apply surgical edit_file changes whose old_string matches exactly once, and recover from match failures.
---

# Precision Editing

## Objective
Land an edit on the first or second attempt, with no whitespace churn.

## Procedure
1. `read_file` the target range immediately before editing. The matcher works on the file's real text, not on
   what the problem statement or an earlier turn claimed.
2. Build `old_string` by **copying 3 to 6 consecutive lines verbatim** from that output, including indentation.
   Choose lines that appear only once in the file — a `def` or `class` line plus the body is usually unique.
3. Build `new_string` as the same block with the minimal change applied. Preserve surrounding lines exactly.
4. `edit_file(filepath, old_string, new_string)`.
5. Check the response: `occurrences` should be 1 and `strategy` `exact`. A `flexible` or `regex` strategy means
   your text did not match character-for-character — read the returned `diff` carefully to confirm the change
   landed where you intended.

## Recovering from FileEditError
- *Could not find match*: your text is wrong, not the file. Re-read that exact line range and copy again. Then
  retry once with a **shorter** `old_string` (2–3 lines around the changed line). Never retry the same string.
- *Multiple matches*: extend `old_string` upward to include the enclosing `def`/`class` line. Use
  `allow_multiple=True` only when every occurrence genuinely must change.
- *File is empty / does not exist*: check the path with `ls` — the file may live under a different package dir.

## Examples

Bad — ambiguous, matches dozens of times:
```python
old_string = "    return None"
```

Good — unique because of the signature line:
```python
old_string = """    def get_token(self, request: Request) -> Optional[str]:
        header = request.headers.get("Authorization")
        if not header:
            return None"""
```

## Rules
- One logical change per `edit_file` call; large payloads risk a truncated tool call.
- Never edit test files or runner configuration — those edits are stripped before scoring.
