#!/usr/bin/env python3
"""Interactive Harness REPL Console.

A live, interactive terminal interface for testing the coding harness:
- Type your turns / instructions and see the agent's thoughts and responses.
- Watch tool calls (read_file, edit_file, run_command, etc.) execute in the sandbox.
- See live colorized unified diffs as soon as code is written or edited.
- Run targeted tests or submit and verify patches against Phase 2 at any point.

Usage:
    .venv/bin/python scripts/interactive_harness.py
    .venv/bin/python scripts/interactive_harness.py --task-id fastapi_15589
    .venv/bin/python scripts/interactive_harness.py --task-idx 0
    .venv/bin/python scripts/interactive_harness.py --task-id fastapi_15589 --mock
"""

from __future__ import annotations

import argparse
import json
import os
import re
import readline
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
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
from scripts.run_gemini_benchmark import extract_snapshot_to_dir, verify_agent_patch


# --- Color Formatting Utilities ---
COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_CYAN = "\033[96m"
COLOR_YELLOW = "\033[93m"
COLOR_BLUE = "\033[94m"
COLOR_BOLD = "\033[1m"
COLOR_DIM = "\033[2m"
COLOR_RESET = "\033[0m"


def format_diff(diff_text: str) -> str:
    """Applies ANSI terminal coloring to unified diff text."""
    if not diff_text:
        return f"{COLOR_DIM}(No changes recorded in working tree){COLOR_RESET}"

    colored_lines = []
    for line in diff_text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            colored_lines.append(f"{COLOR_BOLD}{line}{COLOR_RESET}")
        elif line.startswith("diff --git") or line.startswith("index "):
            colored_lines.append(f"{COLOR_DIM}{line}{COLOR_RESET}")
        elif line.startswith("@@"):
            colored_lines.append(f"{COLOR_CYAN}{line}{COLOR_RESET}")
        elif line.startswith("+"):
            colored_lines.append(f"{COLOR_GREEN}{line}{COLOR_RESET}")
        elif line.startswith("-"):
            colored_lines.append(f"{COLOR_RED}{line}{COLOR_RESET}")
        else:
            colored_lines.append(line)
    return "\n".join(colored_lines)


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
    return ""


class InteractiveSession:
    def __init__(
        self,
        task: Dict[str, Any],
        workspace_dir: Path,
        snapshot_file: Path,
        api_key: str,
        model: str = "gemini-2.5-flash",
        mock_mode: bool = False,
    ):
        self.task = task
        self.workspace = workspace_dir.resolve()
        self.snapshot_file = snapshot_file
        self.api_key = api_key
        self.model = model
        self.mock_mode = mock_mode

        self.tool_calls_count = 0
        self.turn_count = 0
        self.patch_submitted = False
        self.submitted_patch = ""

        # Graph and Embedding Tools
        self.graph: Optional[CodeGraph] = None
        self.emb: Optional[CodeEmbeddingIndex] = None
        self._init_code_intelligence()

        # Conversation history for Gemini API
        self.contents: List[Dict[str, Any]] = []
        self.system_instruction = self.load_system_instruction()
        self.tools_spec = self.get_tool_declarations()

        # Initialize conversation with problem statement
        initial_prompt = self.build_initial_user_prompt()
        self.contents.append({"role": "user", "parts": [{"text": initial_prompt}]})

    def _init_code_intelligence(self):
        repo_clean = self.task["repo"].replace("/", "_")
        base_commit = self.task.get("base_commit", "")
        graph_path = REPO_ROOT / "graphs" / f"{repo_clean}_{base_commit}.json"
        emb_path = REPO_ROOT / "embeddings" / f"{repo_clean}_{base_commit}.npz"
        if graph_path.exists():
            self.graph = CodeGraph(str(graph_path))
        if emb_path.exists():
            self.emb = CodeEmbeddingIndex(str(emb_path))

    def load_system_instruction(self) -> str:
        prompt_file = REPO_ROOT / "archive" / "submission_v2" / "prompts" / "system.md"
        if prompt_file.exists():
            return prompt_file.read_text(encoding="utf-8")
        return "You are an autonomous senior software engineer fixing SWE-bench issues."

    def get_tool_declarations(self) -> List[Dict[str, Any]]:
        schemas_file = REPO_ROOT / "archive" / "submission_v2" / "tools" / "tool_schemas.json"
        if not schemas_file.exists():
            return []
        raw = json.loads(schemas_file.read_text(encoding="utf-8"))
        return [{"name": k, "description": f"Tool: {k}", "parameters": v} for k, v in raw.items()]

    def build_initial_user_prompt(self) -> str:
        repo = self.task.get("repo", "unknown")
        prob = self.task.get("problem_statement", "").strip()
        hints = self.task.get("hints_text", "").strip()
        lines = [
            f"You are evaluating a software engineering task for repository {repo}.",
            "",
            "## Problem Statement:",
            prob,
        ]
        if hints:
            lines.extend(["", "## Hints:", hints])
        lines.extend([
            "",
            "## Rules:",
            "- Work strictly inside /workspace.",
            "- Use read_file to locate code.",
            "- Use edit_file to apply surgical changes.",
            "- Call submit_patch when done.",
        ])
        return "\n".join(lines)

    def _resolve_path(self, filepath: str) -> Path:
        clean = str(filepath).strip()
        if clean.startswith("/workspace/"):
            clean = clean[len("/workspace/"):]
        elif clean == "/workspace":
            clean = "."
        elif clean.startswith("/"):
            clean = clean.lstrip("/")
        target = (self.workspace / clean).resolve()
        if not str(target).startswith(str(self.workspace)):
            raise ValueError(f"Path escapes workspace: {filepath}")
        return target

    def get_live_diff(self) -> str:
        """Returns the current git diff of the workspace, excluding protected files."""
        subprocess.run(["git", "add", "-N", "."], cwd=str(self.workspace), capture_output=True)
        diff_proc = subprocess.run(
            ["git", "diff", "--binary", "HEAD"],
            cwd=str(self.workspace),
            capture_output=True,
            text=True,
        )
        return strip_protected_files_from_patch(diff_proc.stdout)

    def execute_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self.tool_calls_count += 1
        try:
            if name == "read_file":
                fp = args.get("filepath", "")
                try:
                    target = self._resolve_path(fp)
                except ValueError:
                    return {"status": "error", "error_type": "PathTraversalError", "error_message": f"Path escapes workspace: {fp}"}
                if not target.exists():
                    return {"status": "error", "error_type": "FileNotFoundError", "error_message": f"File not found: {fp}"}
                if target.is_dir():
                    return {"status": "error", "error_type": "IsADirectoryError", "error_message": f"Expected file, found directory: {fp}"}

                content = target.read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
                start_l = max(1, int(args.get("start_line") or 1))
                end_l = int(args.get("end_line") or len(lines))
                selected = lines[start_l - 1 : min(end_l, start_l - 1 + 150)]
                chunk = "\n".join(selected)
                if len(chunk) > 10000:
                    chunk = chunk[:10000]
                return {
                    "status": "ok",
                    "filepath": fp,
                    "content": chunk,
                    "start_line": start_l,
                    "end_line": start_l + len(selected) - 1,
                    "total_lines": len(lines),
                }

            elif name == "edit_file":
                fp = args.get("filepath", "")
                old_str = args.get("old_string", "")
                new_str = args.get("new_string", "")
                allow_mult = bool(args.get("allow_multiple", False))
                try:
                    target = self._resolve_path(fp)
                except ValueError:
                    return {"status": "error", "error_type": "PathTraversalError", "error_message": f"Path escapes workspace: {fp}"}
                if not target.exists():
                    return {"status": "error", "error_type": "FileNotFoundError", "error_message": f"File not found: {fp}"}

                content = target.read_text(encoding="utf-8", errors="replace")
                updated, strategy, count = apply_replacement_3tier(content, old_str, new_str, allow_mult)
                target.write_text(updated, encoding="utf-8")
                return {
                    "status": "ok",
                    "filepath": fp,
                    "occurrences": count,
                    "strategy": strategy,
                }

            elif name == "write_file":
                fp = args.get("filepath", "")
                content = args.get("content", "")
                try:
                    target = self._resolve_path(fp)
                except ValueError:
                    return {"status": "error", "error_type": "PathTraversalError", "error_message": f"Path escapes workspace: {fp}"}
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                return {"status": "ok", "filepath": fp, "size": len(content)}

            elif name == "run_command":
                cmd = args.get("command", "")
                cmd = cmd.replace("/workspace", str(self.workspace))
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=str(self.workspace),
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                stdout = proc.stdout[:5000]
                stderr = proc.stderr[:5000]
                return {
                    "status": "ok" if proc.returncode == 0 else "error",
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": proc.returncode,
                }

            elif name == "submit_patch":
                self.submitted_patch = self.get_live_diff()
                self.patch_submitted = True
                files_mod = parse_unified_diff(self.submitted_patch)
                return {
                    "status": "ok",
                    "patch_size": len(self.submitted_patch),
                    "files_changed": len(files_mod),
                }

            elif name == "get_status":
                diff = self.get_live_diff()
                return {
                    "status": "ok",
                    "tool_calls_used": self.tool_calls_count,
                    "patch_submitted": self.patch_submitted,
                    "patch_size": len(diff),
                }

            elif name == "search_similar_code":
                q = args.get("query", "")
                k = int(args.get("k", 5))
                if self.emb:
                    return self.emb.search_similar_code(q, k=k)
                return {"status": "error", "error_message": "Embeddings not indexed for repo"}

            elif name == "get_code_neighbors":
                node = args.get("node", "")
                edge = args.get("edge_type")
                max_n = int(args.get("max_neighbors", 50))
                if self.graph:
                    return self.graph.get_code_neighbors(node, edge_type=edge, max_neighbors=max_n)
                return {"status": "error", "error_message": "AST graph not indexed for repo"}

            elif name == "get_code_subgraph":
                nodes = args.get("nodes", [])
                if self.graph:
                    return self.graph.get_code_subgraph(nodes)
                return {"status": "error", "error_message": "AST graph not indexed for repo"}

            return {"status": "error", "error_type": "UnknownTool", "error_message": f"Unknown tool: {name}"}

        except FileEditError as fee:
            return {"status": "error", "error_type": "FileEditError", "error_message": str(fee)}
        except subprocess.TimeoutExpired:
            return {"status": "error", "error_type": "TimeoutExceeded", "error_message": "Command timed out after 300s"}
        except Exception as e:
            return {"status": "error", "error_type": type(e).__name__, "error_message": str(e)}

    def call_gemini(self) -> Dict[str, Any]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        gen_cfg: Dict[str, Any] = {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
        }
        if "2.5" in self.model:
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 2048}

        payload: Dict[str, Any] = {
            "contents": self.contents,
            "generationConfig": gen_cfg,
        }
        if self.system_instruction:
            payload["system_instruction"] = {
                "parts": [{"text": self.system_instruction}]
            }
        if self.tools_spec:
            payload["tools"] = [{"function_declarations": self.tools_spec}]

        req_body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=req_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=90) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)

    def process_turn(self, user_instruction: str):
        """Sends user instruction to Gemini, executes tool calls in a loop, and displays diffs."""
        self.turn_count += 1
        self.contents.append({"role": "user", "parts": [{"text": user_instruction}]})

        print(f"\n{COLOR_CYAN}Calling {self.model}...{COLOR_RESET}")

        while True:
            t0 = time.perf_counter()
            try:
                resp = self.call_gemini()
                latency = time.perf_counter() - t0
            except urllib.error.HTTPError as he:
                err_body = he.read().decode("utf-8", errors="replace")
                print(f"\n{COLOR_RED}[Gemini API HTTP {he.code} Error]{COLOR_RESET} {err_body}")
                if he.code == 403:
                    print(f"\n{COLOR_YELLOW}💡 TIP: Your API key has an IP address restriction in Google Cloud Console.")
                    print("   To fix:")
                    print("   1. Open Google AI Studio (https://aistudio.google.com/apikey) or Google Cloud Console.")
                    print("   2. Remove the IP restriction (set to 'None') or add your current IP.")
                    print(f"   3. You can still use slash commands: /diff, /test, /run, /edit, /submit{COLOR_RESET}\n")
                return
            except Exception as e:
                print(f"\n{COLOR_RED}[Error calling Gemini API]{COLOR_RESET} {e}")
                return

            candidate = resp.get("candidates", [{}])[0]
            content = candidate.get("content", {})
            parts = content.get("parts", [])

            model_turn_parts = []
            tool_calls = []
            assistant_texts = []

            for part in parts:
                if "text" in part:
                    assistant_texts.append(part["text"])
                    p = {"text": part["text"]}
                    if "thoughtSignature" in part:
                        p["thoughtSignature"] = part["thoughtSignature"]
                    model_turn_parts.append(p)
                elif "functionCall" in part:
                    tool_calls.append(part["functionCall"])
                    p = {"functionCall": part["functionCall"]}
                    if "thoughtSignature" in part:
                        p["thoughtSignature"] = part["thoughtSignature"]
                    model_turn_parts.append(p)

            self.contents.append({"role": "model", "parts": model_turn_parts})

            # Display Model Response
            if assistant_texts:
                full_text = "\n".join(t.strip() for t in assistant_texts if t.strip())
                print(f"\n{COLOR_BOLD}Assistant ({latency:.2f}s):{COLOR_RESET}")
                print(full_text)

            if not tool_calls:
                break

            # Execute tool calls
            tool_responses = []
            file_modified = False

            for fc in tool_calls:
                fn_name = fc.get("name")
                fn_args = fc.get("args", {})
                args_preview = json.dumps(fn_args)[:100]
                print(f"\n{COLOR_YELLOW}⚙️  Executing Tool: {COLOR_BOLD}{fn_name}{COLOR_RESET}({args_preview}...)")

                res = self.execute_tool(fn_name, fn_args)
                status = res.get("status")

                if fn_name in ("edit_file", "write_file") and status == "ok":
                    file_modified = True
                    print(f"   {COLOR_GREEN}✅ File Modified: {fn_args.get('filepath')}{COLOR_RESET}")
                elif fn_name == "run_command":
                    exit_code = res.get("exit_code")
                    code_color = COLOR_GREEN if exit_code == 0 else COLOR_RED
                    print(f"   {code_color}Return Code: {exit_code}{COLOR_RESET}")
                    if res.get("stdout"):
                        print(f"   {COLOR_DIM}STDOUT: {res['stdout'][:300]}...{COLOR_RESET}")
                elif status == "ok":
                    print(f"   {COLOR_GREEN}✓ Status: OK ({len(str(res))} chars returned){COLOR_RESET}")
                else:
                    print(f"   {COLOR_RED}✗ Status: ERROR ({res.get('error_type')}: {res.get('error_message')}){COLOR_RESET}")

                tool_responses.append({
                    "functionResponse": {
                        "name": fn_name,
                        "response": {"name": fn_name, "content": res},
                    }
                })

            self.contents.append({"role": "user", "parts": tool_responses})

            # Auto-display live diff if any file was modified
            if file_modified:
                print(f"\n{COLOR_BOLD}--- LIVE GIT DIFF (Newly Generated Code) ---{COLOR_RESET}")
                print(format_diff(self.get_live_diff()))
                print(f"{COLOR_BOLD}--------------------------------------------{COLOR_RESET}")

            if self.patch_submitted:
                print(f"\n{COLOR_GREEN}🚀 Patch submitted by agent!{COLOR_RESET}")
                break


def run_interactive_repl(task: Dict[str, Any], snapshot_file: Path, model: str = "gemini-2.5-flash", mock: bool = False):
    api_key = ensure_api_key() if not mock else "mock_key"
    inst_id = task["instance_id"]
    repo = task["repo"]

    temp_dir = tempfile.mkdtemp(prefix=f"interactive_{inst_id}_")
    workspace = Path(temp_dir) / "workspace"

    print("=" * 80)
    print(f"{COLOR_BOLD}  SWEGEMMA INTERACTIVE DEVELOPER CONSOLE{COLOR_RESET}")
    print("=" * 80)
    print(f"Task ID:   {COLOR_CYAN}{inst_id}{COLOR_RESET} ({repo})")
    print(f"Model:     {COLOR_YELLOW}{model}{COLOR_RESET}")
    print(f"Sandbox:   {workspace}")
    print(f"Problem:   {task.get('problem_statement', '').strip().split(chr(10))[0]}")
    print("=" * 80)

    print(f"\nUnpacking repository snapshot into isolated sandbox...")
    extract_snapshot_to_dir(snapshot_file, workspace)

    session = InteractiveSession(
        task=task,
        workspace_dir=workspace,
        snapshot_file=snapshot_file,
        api_key=api_key,
        model=model,
        mock_mode=mock,
    )

    print(f"{COLOR_GREEN}Sandbox ready! You can now type instructions or run slash commands.{COLOR_RESET}")
    print(f"{COLOR_DIM}Commands: /diff, /test, /run <cmd>, /cat <file>, /submit, /status, /reset, /help, /exit{COLOR_RESET}\n")

    try:
        while True:
            try:
                user_input = input(f"{COLOR_BOLD}(harness) [Turn {session.turn_count + 1}] > {COLOR_RESET}").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting session...")
                break

            if not user_input:
                continue

            # --- Slash Commands ---
            cmd_lower = user_input.lower()
            if cmd_lower in ("/exit", "/quit", "exit", "quit"):
                print("Exiting interactive console. Cleaning up sandbox...")
                break

            elif cmd_lower in ("/diff", "diff"):
                live_diff = session.get_live_diff()
                print(f"\n{COLOR_BOLD}=== CURRENT LIVE PATCH DIFF ({len(live_diff)} bytes) ==={COLOR_RESET}")
                print(format_diff(live_diff))
                print(f"{COLOR_BOLD}==================================================={COLOR_RESET}\n")
                continue

            elif cmd_lower.startswith("/run "):
                shell_cmd = user_input[5:].strip()
                print(f"{COLOR_CYAN}Running bash in sandbox: {shell_cmd}{COLOR_RESET}")
                res = session.execute_tool("run_command", {"command": shell_cmd})
                print(f"Exit Code: {res.get('exit_code')}")
                if res.get("stdout"):
                    print(res["stdout"])
                if res.get("stderr"):
                    print(f"{COLOR_RED}{res['stderr']}{COLOR_RESET}")
                continue

            elif cmd_lower.startswith("/test") or cmd_lower == "test":
                parts = user_input.split(maxsplit=1)
                test_target = parts[1] if len(parts) > 1 else ""
                pytest_bin = str(REPO_ROOT / ".venv" / "bin" / "pytest")
                cmd = f"{pytest_bin} {test_target} -q".strip()
                print(f"{COLOR_CYAN}Running targeted test: {cmd}{COLOR_RESET}")
                res = session.execute_tool("run_command", {"command": cmd})
                if res.get("stdout"):
                    print(res["stdout"])
                if res.get("stderr"):
                    print(f"{COLOR_RED}{res['stderr']}{COLOR_RESET}")
                print(f"Test Exit Code: {res.get('exit_code')}\n")
                continue

            elif cmd_lower.startswith("/cat ") or cmd_lower.startswith("/read "):
                target_file = user_input.split()[1]
                res = session.execute_tool("read_file", {"filepath": target_file, "start_line": 1, "end_line": 150})
                if res.get("status") == "ok":
                    print(f"\n--- {target_file} (Lines 1-{res.get('end_line')} of {res.get('total_lines')}) ---")
                    print(res.get("content"))
                    print("-" * 50)
                else:
                    print(f"{COLOR_RED}Error: {res.get('error_message')}{COLOR_RESET}")
                continue

            elif cmd_lower.startswith("/patch "):
                p_path = user_input[7:].strip()
                target_p = Path(p_path).expanduser().resolve()
                if not target_p.exists():
                    print(f"{COLOR_RED}Patch file not found: {p_path}{COLOR_RESET}")
                    continue
                p_content = target_p.read_text(encoding="utf-8")
                patch_tmp = workspace / "_manual.patch"
                patch_tmp.write_text(p_content, encoding="utf-8")
                res = subprocess.run(["git", "apply", "--unsafe-paths", str(patch_tmp)], cwd=str(workspace), capture_output=True, text=True)
                patch_tmp.unlink(missing_ok=True)
                if res.returncode == 0:
                    print(f"{COLOR_GREEN}Patch {target_p.name} applied successfully!{COLOR_RESET}")
                    print(f"\n{COLOR_BOLD}=== LIVE GIT DIFF ==={COLOR_RESET}")
                    print(format_diff(session.get_live_diff()))
                    print(f"{COLOR_BOLD}====================={COLOR_RESET}\n")
                else:
                    print(f"{COLOR_RED}Patch failed to apply: {res.stderr}{COLOR_RESET}")
                continue

            elif cmd_lower in ("/submit", "submit"):
                live_patch = session.get_live_diff()
                print(f"\n{COLOR_BOLD}--- SUBMITTING LIVE PATCH ({len(live_patch)} bytes) ---{COLOR_RESET}")
                print(format_diff(live_patch))
                print(f"\n{COLOR_CYAN}Triggering Phase 2 Hermetic Verification Sandbox (Container B)...{COLOR_RESET}")
                verif_res = verify_agent_patch(task, snapshot_file, live_patch)
                print("\n" + "=" * 60)
                print(f"  PHASE 2 VERIFICATION RESULT: {'✅ PASS (1.0)' if verif_res.get('resolved') else '❌ FAIL (0.0)'}")
                print(f"  Pytest Exit Code: {verif_res.get('exit_code')}")
                print("=" * 60 + "\n")
                continue

            elif cmd_lower in ("/status", "status"):
                live_diff = session.get_live_diff()
                files = parse_unified_diff(live_diff)
                print(f"\n{COLOR_BOLD}Session Status:{COLOR_RESET}")
                print(f"  Turns Executed:     {session.turn_count}")
                print(f"  Tool Calls Used:    {session.tool_calls_count}")
                print(f"  Live Diff Size:     {len(live_diff)} bytes")
                print(f"  Files Modified:     {[f['new_file'] for f in files]}\n")
                continue

            elif cmd_lower in ("/reset", "reset"):
                print(f"{COLOR_YELLOW}Resetting working tree back to clean baseline...{COLOR_RESET}")
                session.execute_tool("run_command", {"command": "git checkout HEAD -- . && git clean -fd"})
                print("Working tree reset.\n")
                continue

            elif cmd_lower in ("/help", "help"):
                print(f"""
{COLOR_BOLD}Available Commands:{COLOR_RESET}
  <any text>          Send instruction/turn to Gemini agent (e.g. "Read utils.py and fix the header bug")
  /diff               Show current live git diff with syntax coloring
  /test [target]      Run pytest inside the sandbox on target test file
  /cat <path>         Inspect lines 1-150 of a file in the workspace
  /run <command>      Execute arbitrary bash command inside the sandbox
  /submit             Submit current patch and run Phase 2 verification sandbox
  /status             Display turn count, tool calls used, and modified files
  /reset              Discard changes and revert sandbox to baseline
  /exit or /quit      Exit and clean up sandbox
""")
                continue

            # --- Agent Turn Execution ---
            if session.mock_mode:
                print(f"{COLOR_YELLOW}[Mock Mode] Type '/diff' to see current diff or use slash commands.{COLOR_RESET}")
                continue

            session.process_turn(user_input)

    finally:
        print(f"Cleaning up sandbox: {temp_dir}...")
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Interactive Developer Console for SWE-bench Harness.")
    parser.add_argument("--task-id", type=str, default=None, help="Task instance ID (default: fastapi_15589)")
    parser.add_argument("--task-idx", type=int, default=None, help="0-based index in tasks.jsonl")
    parser.add_argument("--model", type=str, default="gemini-2.5-flash", help="Gemini model (default: gemini-2.5-flash)")
    parser.add_argument("--mock", action="store_true", help="Launch in offline mock mode (no API calls)")
    args = parser.parse_args()

    tasks_path = REPO_ROOT / "tasks.jsonl"
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

    run_interactive_repl(
        task=target_task,
        snapshot_file=snap_file,
        model=args.model,
        mock=args.mock,
    )


if __name__ == "__main__":
    main()
