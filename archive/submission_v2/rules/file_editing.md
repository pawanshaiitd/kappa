# Precision File Editing Rules

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
