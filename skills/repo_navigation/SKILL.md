---
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
