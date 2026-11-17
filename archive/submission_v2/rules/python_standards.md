# Python Engineering Standards

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
