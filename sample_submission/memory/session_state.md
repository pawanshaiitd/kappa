# Session State Architecture

ADK sessions persist key-value variables across multi-turn executions in `session.state`:

## Managed State Variables
- `problem_description` (`str`): Task issue statement provided by benchmark harness.
- `hints` (`str | None`): Community or author hints for target issue.
- `active_phase` (`"PLANNING" | "EXECUTION" | "VERIFICATION"`): State-machine gate to prevent premature edits.
- `target_files` (`list[str]`): Discovered files requiring surgical modifications.
- `edited_files` (`list[str]`): Tracking files modified during the session.
- `last_test_command` (`str`): The specific targeted pytest command used for validation.
- `patch_submitted` (`bool`): Flag set to true upon successful `submit_patch` invocation.
