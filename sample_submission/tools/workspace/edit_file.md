# Tool: `edit_file`

## Signature
`edit_file(filepath: str, old_string: str, new_string: str, allow_multiple: bool = False) -> str`

## 3-Tier Matcher Algorithm
1. **Tier 1 (Exact)**: Direct substring matching.
2. **Tier 2 (Flexible)**: Strips leading/trailing whitespace per line, matches code structure, and re-indents `new_string` to the baseline indentation of the match.
3. **Tier 3 (Regex)**: Tokenizes `old_string` around code delimiters and allows flexible whitespace.

## Uniqueness Requirement
If `old_string` matches more than 1 location and `allow_multiple=False`, the tool aborts with `FileEditError: Multiple matches found`. Provide 3+ lines of unique context to guarantee single-site targeting.
