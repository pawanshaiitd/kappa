---
name: repo-navigation
description: Locate the file and line range responsible for an issue using grep and targeted reads, without broad sweeps.
---

# Repository Navigation

## Objective
Get from a problem statement to the exact file and line range in two or three tool calls.

## Procedure
1. Pick the most distinctive token in the problem statement: a parameter name, flag, class, exception message,
   or a quoted string. Prefer a token that is unlikely to appear in prose.
2. Grep the package directory only:
   `grep -rn "<token>" --include=*.py <package_dir> | head -40`
   Take `<package_dir>` from the workspace layout in the task prompt. Never grep `.` (it walks `docs/`,
   `tests/` and `.git`), and never grep `docs/`, `docs_src/`, `examples/` or `tests/`.
3. If there are many hits, narrow with a second token:
   `grep -rn "<token>" --include=*.py <package_dir> | grep -i "<second_token>"`
4. Read around the best hit: `read_file(path, start_line=<hit-40>, end_line=<hit+40>)`.
5. Only when grep finds nothing useful, fall back to `search_similar_code(query="<symbol_name>")`, which
   matches dotted symbol paths rather than free text.

## Anti-patterns
- `find /workspace -name "*.py"` — thousands of lines, no signal.
- `grep -r` without `--include=*.py` — matches documentation and translations.
- Reading a file from line 1 hoping the relevant code is in the first 150 lines.
