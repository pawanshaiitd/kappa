#!/usr/bin/env python3
"""Interactive Manual Testing CLI for the Coding Harness.

Allows developers to manually test and inspect the harness, sandbox, and Gemini agent:
1. Select any task from tasks.jsonl (FastAPI, Rich, Requests, HTTPX) or pass a custom issue.
2. Inspect how the agent searches the codebase, reads files, and writes code.
3. Run step-by-step with interactive inspection (--step) or full autonomous execution.
4. Verify the generated code and test patch in the isolated verification sandbox.

Usage:
    .venv/bin/python scripts/manual_test.py --list
    .venv/bin/python scripts/manual_test.py --task-id fastapi_15589
    .venv/bin/python scripts/manual_test.py --task-idx 2 --step
    .venv/bin/python scripts/manual_test.py --custom-repo fastapi --custom-prompt "Add a hello_world endpoint to FastAPI"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.harness_tools import (
    apply_replacement_3tier,
    FileEditError,
    CodeGraph,
    CodeEmbeddingIndex,
    parse_unified_diff,
    strip_protected_files_from_patch,
)
from scripts.run_gemini_benchmark import extract_snapshot_to_dir, verify_agent_patch, GeminiAgentRunner


def ensure_api_key() -> str:
    """Finds GOOGLE_API_KEY or GEMINI_API_KEY from environment or ~/.zshrc."""
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if key:
        return key

    zshrc = Path.home() / ".zshrc"
    if zshrc.exists():
        content = zshrc.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r'export\s+(GOOGLE_API_KEY|GEMINI_API_KEY)\s*=\s*["\']?([^"\'\s\n]+)["\']?', content):
            found_key = match.group(2)
            if found_key:
                os.environ[match.group(1)] = found_key
                return found_key

    print("[Error] No Gemini API key found in environment or ~/.zshrc.")
    print("Please set your key in ~/.zshrc:")
    print("  export GOOGLE_API_KEY=\"AIzaSy...\"")
    sys.exit(1)


def list_tasks(tasks_path: Path):
    print("=" * 80)
    print("  AVAILABLE BENCHMARK TASKS FOR MANUAL TESTING")
    print("=" * 80)
    with open(tasks_path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            t = json.loads(line)
            inst_id = t.get("instance_id")
            repo = t.get("repo")
            title = t.get("problem_statement", "").strip().split("\n")[0][:70]
            snap_file = REPO_ROOT / "snapshots" / f"{inst_id}.tgz"
            snap_status = "Available" if snap_file.exists() else "Missing snapshot"
            print(f"[{idx:>3}] {inst_id:<16} | {repo:<18} | {snap_status:<16} | {title}")
            if idx >= 20:
                print(f"    ... and {sum(1 for _ in f)} more tasks. Use --task-id <id> or --task-idx <idx> to run.")
                break
    print("=" * 80)


class ManualInteractiveRunner(GeminiAgentRunner):
    """Interactive extension of GeminiAgentRunner supporting manual stepping, debugging, and offline simulation."""

    def __init__(self, *args, step_mode: bool = False, mock_mode: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.step_mode = step_mode
        self.mock_mode = mock_mode

    def _get_mock_step(self, turn: int) -> Dict[str, Any]:
        """Provides realistic simulation turns for testing harness tool execution offline."""
        inst_id = self.task["instance_id"]
        if inst_id == "fastapi_15589":
            if turn == 1:
                return {
                    "text": "I will locate headers handling in FastAPI by searching code embeddings.",
                    "tool_calls": [{"name": "search_similar_code", "args": {"query": "headers convert_underscores", "k": 3}}]
                }
            elif turn == 2:
                return {
                    "text": "I will inspect request_params_to_args in fastapi/dependencies/utils.py.",
                    "tool_calls": [{"name": "read_file", "args": {"filepath": "fastapi/dependencies/utils.py", "start_line": 820, "end_line": 845}}]
                }
            elif turn == 3:
                return {
                    "text": "Applying surgical modification to skip underscore headers when convert_underscores=True.",
                    "tool_calls": [{
                        "name": "edit_file",
                        "args": {
                            "filepath": "fastapi/dependencies/utils.py",
                            "old_string": "    for key in received_params.keys():\n        if key not in processed_keys:\n            if isinstance(received_params, (ImmutableMultiDict, Headers)):",
                            "new_string": "    for key in received_params.keys():\n        if key not in processed_keys:\n            if default_convert_underscores and isinstance(received_params, Headers) and \"_\" in key:\n                continue\n            if isinstance(received_params, (ImmutableMultiDict, Headers)):",
                        }
                    }]
                }
            elif turn == 4:
                return {
                    "text": "Running targeted test to verify the fix.",
                    "tool_calls": [{"name": "run_command", "args": {"command": "pytest tests/test_query_cookie_header_model_extra_params.py -q"}}]
                }
            elif turn == 5:
                return {
                    "text": "The targeted test passed. Submitting final patch.",
                    "tool_calls": [{"name": "submit_patch", "args": {}}]
                }
        # Generic mock fallback
        if turn == 1:
            return {
                "text": "Searching repository symbol graph.",
                "tool_calls": [{"name": "search_similar_code", "args": {"query": "main", "k": 2}}]
            }
        return {
            "text": "Submitting patch.",
            "tool_calls": [{"name": "submit_patch", "args": {}}]
        }

    def run_agent_loop(self) -> Dict[str, Any]:
        print(f"\n[Manual Test Session Started]")
        print(f"Model:     {'OFFLINE SIMULATOR (--mock)' if self.mock_mode else self.model}")
        print(f"Workspace: {self.workspace}")
        print(f"Step Mode: {'ON (Press Enter between turns)' if self.step_mode else 'OFF (Autonomous)'}")
        print("=" * 75)

        sys_inst = self.load_system_instruction()
        tools_spec = self.get_tool_declarations()
        initial_user = self.build_initial_user_prompt()

        contents: List[Dict[str, Any]] = [
            {"role": "user", "parts": [{"text": initial_user}]}
        ]

        turn = 0
        while turn < self.max_turns and not self.patch_submitted:
            turn += 1
            print(f"\n" + "-" * 75)
            print(f"  >>> TURN {turn}/{self.max_turns} <<<")
            print("-" * 75)

            if self.step_mode:
                try:
                    user_input = input("Press [Enter] to execute turn (or type 'quit' / 'patch'): ").strip()
                    if user_input.lower() == "quit":
                        print("[Manual Test] Session aborted by user.")
                        break
                    elif user_input.lower() == "patch":
                        print("[Manual Test] Forcing patch submission...")
                        self.execute_tool("submit_patch", {})
                        break
                except (EOFError, KeyboardInterrupt):
                    print("\n[Manual Test] Exiting.")
                    break

            if self.mock_mode:
                step_data = self._get_mock_step(turn)
                text_in_turn = [step_data["text"]]
                tool_calls_in_turn = step_data.get("tool_calls", [])
                latency = 0.05
            else:
                try:
                    t0 = time.perf_counter()
                    resp = self.call_gemini(contents, sys_inst, tools_spec)
                    latency = time.perf_counter() - t0
                except Exception as e:
                    print(f"\n[Gemini API Error] {e}")
                    if "403" in str(e) or "PERMISSION_DENIED" in str(e):
                        print("\n💡 TIP: Your API key has an IP address restriction in Google Cloud Console.")
                        print("   To fix:")
                        print("   1. Open Google AI Studio (https://aistudio.google.com/apikey) or Google Cloud Console.")
                        print("   2. Remove the IP restriction (set to 'None') or add your current IP (24.239.142.52).")
                        print("   3. Or test offline immediately with: .venv/bin/python scripts/manual_test.py --mock\n")
                    break

                candidate = resp.get("candidates", [{}])[0]
                content = candidate.get("content", {})
                parts = content.get("parts", [])

                model_turn_parts = []
                tool_calls_in_turn = []
                text_in_turn = []

                for part in parts:
                    if "text" in part:
                        text_in_turn.append(part["text"])
                        p = {"text": part["text"]}
                        if "thoughtSignature" in part:
                            p["thoughtSignature"] = part["thoughtSignature"]
                        model_turn_parts.append(p)
                    elif "functionCall" in part:
                        tool_calls_in_turn.append(part["functionCall"])
                        p = {"functionCall": part["functionCall"]}
                        if "thoughtSignature" in part:
                            p["thoughtSignature"] = part["thoughtSignature"]
                        model_turn_parts.append(p)

                contents.append({"role": "model", "parts": model_turn_parts})

            if text_in_turn:
                summary = "\n".join(t.strip() for t in text_in_turn if t.strip())
                print(f"[Model Output ({latency:.2f}s)]:\n{summary}\n")

            if not tool_calls_in_turn:
                if self.patch_submitted:
                    print("[Session Complete] Patch has been submitted.")
                    break
                print("[Continuation Nudge] Injecting prompt to prompt next action...")
                contents.append({
                    "role": "user",
                    "parts": [{"text": "Please continue your work using the available tools, or call submit_patch when you have completed and verified your changes."}]
                })
                continue

            tool_response_parts = []
            for fc in tool_calls_in_turn:
                fn_name = fc.get("name")
                fn_args = fc.get("args", {})
                args_str = json.dumps(fn_args, indent=2) if len(json.dumps(fn_args)) > 80 else json.dumps(fn_args)
                print(f"⚙️  Tool Call: {fn_name}")
                print(f"   Arguments: {args_str}")

                tool_res = self.execute_tool(fn_name, fn_args)
                status = tool_res.get("status")

                if fn_name == "edit_file" and status == "ok":
                    print(f"   ✅ [File Edited] {fn_args.get('filepath')} (strategy: {tool_res.get('strategy')})")
                elif fn_name == "write_file" and status == "ok":
                    print(f"   ✅ [File Written] {fn_args.get('filepath')} ({tool_res.get('size')} bytes)")
                elif fn_name == "submit_patch" and status == "ok":
                    print(f"   🚀 [Patch Submitted] Size: {tool_res.get('patch_size')} bytes | Files: {tool_res.get('files_changed')}")
                elif status == "ok":
                    print(f"   ✓ [Status OK] Output length: {len(str(tool_res))} chars")
                else:
                    print(f"   ⚠️  [Status ERROR]: {tool_res.get('error_type')}: {tool_res.get('error_message')}")

                remaining_turns = self.max_turns - turn
                if remaining_turns <= 5 and not self.patch_submitted and isinstance(tool_res, dict):
                    tool_res["budget_warning"] = (
                        f"Only {remaining_turns} turn(s) remaining ({turn}/{self.max_turns} used). "
                        "Finalize your edits with edit_file and call submit_patch immediately."
                    )

                tool_response_parts.append({
                    "functionResponse": {
                        "name": fn_name,
                        "response": {"name": fn_name, "content": tool_res},
                    }
                })

            contents.append({"role": "user", "parts": tool_response_parts})

            if self.patch_submitted:
                print("\n[Submit Patch Detected] Finished agent problem solving!")
                break

        # Fallback patch extraction
        if not self.submitted_patch:
            subprocess.run(["git", "add", "-N", "."], cwd=str(self.workspace), capture_output=True)
            diff_proc = subprocess.run(
                ["git", "diff", "--binary", "HEAD"],
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
            )
            self.submitted_patch = strip_protected_files_from_patch(diff_proc.stdout)

        return {
            "instance_id": self.task["instance_id"],
            "repo": self.task["repo"],
            "turns_taken": turn,
            "tool_calls_count": self.tool_calls_count,
            "patch_submitted": self.patch_submitted,
            "patch_size": len(self.submitted_patch),
            "patch": self.submitted_patch,
        }


def run_manual_test(
    task: Dict[str, Any],
    snapshot_file: Path,
    model: str = "gemini-2.5-flash",
    max_turns: int = 15,
    step_mode: bool = False,
    mock_mode: bool = False,
    verify: bool = True,
):
    api_key = "mock_key" if mock_mode else ensure_api_key()
    inst_id = task["instance_id"]
    repo = task["repo"]

    print("\n" + "=" * 75)
    print(f"  MANUAL TEST RUN: {inst_id} ({repo})")
    print(f"  Problem: {task.get('problem_statement', '').strip().split(chr(10))[0]}")
    print("=" * 75)

    temp_dir = tempfile.mkdtemp(prefix=f"manual_test_{inst_id}_")
    workspace = Path(temp_dir) / "workspace"

    try:
        print(f"Extracting repository snapshot into isolated sandbox: {workspace}...")
        extract_snapshot_to_dir(snapshot_file, workspace)

        runner = ManualInteractiveRunner(
            task=task,
            workspace_dir=workspace,
            api_key=api_key,
            model=model,
            max_turns=max_turns,
            step_mode=step_mode,
            mock_mode=mock_mode,
        )

        result = runner.run_agent_loop()
        agent_patch = result["patch"]

        print("\n" + "=" * 75)
        print("  STAGE 1 COMPLETE: EXTRACTED DIFF")
        print("=" * 75)
        if agent_patch.strip():
            print(f"Unified Diff ({len(agent_patch)} bytes):\n")
            print(agent_patch)
        else:
            print("No changes made by the agent.")

        # Phase 2 Verification
        if verify:
            verif_res = verify_agent_patch(task, snapshot_file, agent_patch)
            print("\n" + "=" * 75)
            print("  MANUAL TEST FINAL VERIFICATION RESULT")
            print("=" * 75)
            print(f"Task ID:          {inst_id}")
            print(f"Resolved:         {'✅ PASS (1.0)' if verif_res.get('resolved') else '❌ FAIL (0.0)'}")
            print(f"Exit Code:        {verif_res.get('exit_code')}")
            print(f"Turns Taken:      {result['turns_taken']}")
            print(f"Tool Calls Count: {result['tool_calls_count']}")
            print("=" * 75)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Manual Testing CLI for swegemma coding harness.")
    parser.add_argument("--list", action="store_true", help="List available benchmark tasks")
    parser.add_argument("--task-id", type=str, default=None, help="Task instance ID (e.g. fastapi_15589)")
    parser.add_argument("--task-idx", type=int, default=None, help="0-based index in tasks.jsonl")
    parser.add_argument("--model", type=str, default="gemini-2.5-flash", help="Gemini model (default: gemini-2.5-flash)")
    parser.add_argument("--max-turns", type=int, default=15, help="Max turns allowance")
    parser.add_argument("--step", action="store_true", help="Interactive step-by-step mode (press Enter between turns)")
    parser.add_argument("--mock", action="store_true", help="Run offline simulation mode without network/API calls")
    parser.add_argument("--no-verify", action="store_true", help="Skip Phase 2 test verification")
    args = parser.parse_args()

    tasks_path = REPO_ROOT / "tasks.jsonl"
    if args.list:
        list_tasks(tasks_path)
        return

    with open(tasks_path, "r", encoding="utf-8") as f:
        tasks = [json.loads(line) for line in f if line.strip()]

    target_task = None
    if args.task_id:
        for t in tasks:
            if t["instance_id"] == args.task_id:
                target_task = t
                break
        if not target_task:
            print(f"[Error] Task {args.task_id} not found.")
            sys.exit(1)
    elif args.task_idx is not None:
        if 0 <= args.task_idx < len(tasks):
            target_task = tasks[args.task_idx]
        else:
            print(f"[Error] Index {args.task_idx} out of range.")
            sys.exit(1)
    else:
        # Default to fastapi_15589 (index 2)
        target_task = tasks[2]

    inst_id = target_task["instance_id"]
    snap_file = REPO_ROOT / "snapshots" / f"{inst_id}.tgz"
    if not snap_file.exists():
        print(f"[Error] Snapshot archive missing: {snap_file}")
        sys.exit(1)

    run_manual_test(
        task=target_task,
        snapshot_file=snap_file,
        model=args.model,
        max_turns=args.max_turns,
        step_mode=args.step,
        mock_mode=args.mock,
        verify=not args.no_verify,
    )


if __name__ == "__main__":
    main()
