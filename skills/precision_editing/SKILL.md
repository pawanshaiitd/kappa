---
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
