# Finding the package under test in an unfamiliar repository

Never assume a directory name. Derive it:

```
run_command: ls
run_command: ls */__init__.py 2>/dev/null; ls src/*/__init__.py 2>/dev/null
```

The importable package is the directory holding `__init__.py`. Two layouts cover almost everything:

- **flat layout** — the package sits at the repository root (`<pkg>/__init__.py`)
- **src layout** — it sits under `src/` (`src/<pkg>/__init__.py`)

Confirm which name the tests import:

```
run_command: head -20 tests/*.py | grep -E "^(from|import) " | sort -u | head
```

## Directories to exclude from every search

`docs/`, `docs_src/`, `examples/`, `benchmarks/`, `tests/`, `.git/`, `__pycache__/`, and any
translation or locale directory. They contain text that matches your search terms but is never the
code under test. Always pass `--include=*.py` and a specific directory, never `.`.

## When the symbol is not where you expect

- A re-export: the name is defined in one module and exposed from `__init__.py`. Search for the
  `def`/`class` definition, not the import: `grep -rn "def <name>\|class <name>" --include=*.py <pkg>`
- A behaviour with no obvious symbol: find where it was introduced with
  `git log -S"<string>" --oneline -- <pkg> | head`, then read that commit's diff.
- Nothing matches: shorten the search string. Quoted text from the problem statement is often
  reformatted in the source; try its most distinctive single word.
