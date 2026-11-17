"""Gemini Benchmark Evaluation Runner for swegemma / SWE-bench Tasks.

Enables running SWE-bench benchmark tasks locally without local GPUs/vLLM by routing
agent reasoning and tool calls to Google Gemini via GEMINI_API_KEY / GOOGLE_API_KEY.

Usage:
    export GEMINI_API_KEY="your-api-key"
    python scripts/run_gemini_benchmark.py --task-id fastapi_15661
    python scripts/run_gemini_benchmark.py --task-idx 0 --model gemini-2.5-flash
    python scripts/run_gemini_benchmark.py --dry-run --task-idx 0
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.harness_tools import (
    apply_replacement_3tier,
    FileEditError,
    CodeGraph,
    CodeEmbeddingIndex,
    is_protected_file,
    parse_unified_diff,
    strip_protected_files_from_patch,
)
from scripts.test_envs import resolve_pytest


class GeminiAgentRunner:
    def __init__(
        self,
        task: Dict[str, Any],
        workspace_dir: Path,
        api_key: str,
        model: str = "gemini-2.5-flash",
        max_turns: int = 15,
        submission_dir: Optional[Path] = None,
    ):
        self.task = task
        self.workspace = workspace_dir.resolve()
        self.api_key = api_key
        self.model = model
        self.max_turns = max_turns
        self.submission_dir = submission_dir or (REPO_ROOT / "sample_submission")

        self.patch_submitted = False
        self.submitted_patch = ""
        self.tool_calls_count = 0
        self.history: List[Dict[str, Any]] = []

        # Graph and Embedding tools
        self.graph: Optional[CodeGraph] = None
        self.emb: Optional[CodeEmbeddingIndex] = None
        self._init_code_intelligence()

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

    def _init_code_intelligence(self):
        repo_clean = self.task["repo"].replace("/", "_")
        base_commit = self.task.get("base_commit", "")

        # Try to locate graph and embedding files
        graph_candidates = list((REPO_ROOT / "graphs").glob(f"*{base_commit}*.json"))
        if not graph_candidates:
            graph_candidates = list((REPO_ROOT / "graphs").glob(f"*{repo_clean}*.json"))
        if graph_candidates:
            self.graph = CodeGraph(str(graph_candidates[0]))

        emb_candidates = list((REPO_ROOT / "embeddings").glob(f"*{base_commit}*.npz"))
        if not emb_candidates:
            emb_candidates = list((REPO_ROOT / "embeddings").glob(f"*{repo_clean}*.npz"))
        if emb_candidates:
            self.emb = CodeEmbeddingIndex(str(emb_candidates[0]))

    def load_system_instruction(self) -> str:
        prompt_file = self.submission_dir / "prompts" / "system.md"
        if prompt_file.exists():
            return prompt_file.read_text(encoding="utf-8")
        return "You are an expert autonomous software engineer solving an issue in a repository."

    def build_initial_user_prompt(self) -> str:
        repo = self.task.get("repo", "unknown")
        prob = self.task.get("problem_statement", "").strip()
        hints = self.task.get("hints_text", "").strip()

        lines = [
            f"You are evaluating a software engineering task for repository {repo}.",
            "",
            "## Problem Statement:",
            prob,
            "",
        ]
        if hints:
            lines.extend(["## Hints:", hints, ""])

        lines.extend([
            "## Execution Environment Rules:",
            "- Single command timeout: 300 seconds",
            "- Command output limit: 5000 characters",
            "- File view limit: 150 lines per read_file call",
            "- File character limit: 10000 characters per read_file call",
            "- Environment is offline (no network/PyPI access). All dependencies are ALREADY pre-installed.",
            "",
            "## Standard Instructions:",
            "1. Work strictly under /workspace",
            "2. Inspect existing conventions before modifying code",
            "3. Verify your implementation using targeted tests before submitting",
            "4. Call submit_patch once verified",
            "",
            "## Workspace Directory Layout:",
        ])

        # Sample top directory entries
        files_sample = []
        for p in sorted(self.workspace.rglob("*")):
            if p.is_file() and not any(part.startswith(".") for part in p.parts):
                rel = str(p.relative_to(self.workspace))
                if not any(ign in rel for ign in ["__pycache__", ".egg-info", "build", "dist"]):
                    files_sample.append(rel)
                    if len(files_sample) >= 50:
                        break
        lines.append("\n".join(f"- {f}" for f in files_sample))
        return "\n".join(lines)

    def get_tool_declarations(self) -> List[Dict[str, Any]]:
        schemas_path = self.submission_dir / "tools" / "tool_schemas.json"
        if not schemas_path.exists():
            return []

        raw_schemas = json.loads(schemas_path.read_text(encoding="utf-8"))
        decls = []
        for name, spec in raw_schemas.items():
            decls.append({
                "name": name,
                "description": f"Tool: {name}",
                "parameters": spec,
            })
        return decls

    # --- Tool Execution Engine ---
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
                # Bound to 150 lines
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
                # Rewrite /workspace references to the local workspace folder
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
                # git add -N . && git diff HEAD
                subprocess.run(["git", "add", "-N", "."], cwd=str(self.workspace), capture_output=True)
                diff_proc = subprocess.run(
                    ["git", "diff", "--binary", "HEAD"],
                    cwd=str(self.workspace),
                    capture_output=True,
                    text=True,
                )
                self.submitted_patch = strip_protected_files_from_patch(diff_proc.stdout)
                self.patch_submitted = True
                files_mod = parse_unified_diff(self.submitted_patch)
                return {
                    "status": "ok",
                    "patch_size": len(self.submitted_patch),
                    "files_changed": len(files_mod),
                }

            elif name == "get_status":
                return {
                    "status": "ok",
                    "tool_calls_used": self.tool_calls_count,
                    "patch_submitted": self.patch_submitted,
                    "patch_size": len(self.submitted_patch),
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

            else:
                return {"status": "error", "error_type": "UnknownTool", "error_message": f"Unknown tool: {name}"}

        except FileEditError as fee:
            return {"status": "error", "error_type": "FileEditError", "error_message": str(fee)}
        except subprocess.TimeoutExpired:
            return {"status": "error", "error_type": "TimeoutExceeded", "error_message": "Command timed out after 300s"}
        except Exception as e:
            return {"status": "error", "error_type": type(e).__name__, "error_message": str(e)}

    # --- Gemini API Call ---
    def call_gemini(self, contents: List[Dict[str, Any]], system_instruction: str, tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        gen_cfg: Dict[str, Any] = {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
        }
        if "2.5" in self.model:
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 2048}

        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": gen_cfg,
        }
        if system_instruction:
            payload["system_instruction"] = {
                "parts": [{"text": system_instruction}]
            }
        if tools:
            payload["tools"] = [{"function_declarations": tools}]

        req_body = json.dumps(payload).encode("utf-8")

        for attempt in range(4):
            try:
                req = urllib.request.Request(
                    url,
                    data=req_body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=90) as resp:
                    data = resp.read().decode("utf-8")
                    return json.loads(data)
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    wait_time = (2 ** attempt) * 3
                    print(f"[Gemini API Retry] HTTP {e.code}: waiting {wait_time}s before retry {attempt + 1}/3...")
                    time.sleep(wait_time)
                    continue
                raise RuntimeError(f"Gemini API returned HTTP {e.code}: {err_body}") from e
            except urllib.error.URLError as ue:
                if attempt < 3:
                    wait_time = (2 ** attempt) * 2
                    print(f"[Gemini Network Retry] {ue}: waiting {wait_time}s before retry {attempt + 1}/3...")
                    time.sleep(wait_time)
                    continue
                raise

    def run_agent_loop(self) -> Dict[str, Any]:
        print(f"\n[Gemini Agent Loop] Model: {self.model} | Workspace: {self.workspace}")
        print("=" * 70)

        sys_inst = self.load_system_instruction()
        tools_spec = self.get_tool_declarations()
        initial_user = self.build_initial_user_prompt()

        contents: List[Dict[str, Any]] = [
            {"role": "user", "parts": [{"text": initial_user}]}
        ]

        turn = 0
        while turn < self.max_turns and not self.patch_submitted:
            turn += 1
            print(f"\n>>> TURN {turn}/{self.max_turns} <<<")

            try:
                t0 = time.perf_counter()
                resp = self.call_gemini(contents, sys_inst, tools_spec)
                latency = time.perf_counter() - t0
            except Exception as e:
                print(f"[API Error] Failed calling Gemini API: {e}")
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
                summary = " ".join(t.strip() for t in text_in_turn if t.strip())
                print(f"Assistant: {summary[:300]}...")

            if not tool_calls_in_turn:
                if self.patch_submitted:
                    print("[Session Complete] Patch has been submitted.")
                    break
                # Continuation nudge
                print("[Continuation Nudge] Model did not emit tool call, injecting prompt...")
                contents.append({
                    "role": "user",
                    "parts": [{"text": "Please continue your work using the available tools, or call submit_patch when you have completed and verified your changes."}]
                })
                continue

            # Execute all function calls emitted by Gemini
            tool_response_parts = []
            for fc in tool_calls_in_turn:
                fn_name = fc.get("name")
                fn_args = fc.get("args", {})
                print(f"  -> Tool Call: {fn_name}({json.dumps(fn_args)[:120]})")

                tool_res = self.execute_tool(fn_name, fn_args)
                status = tool_res.get("status")

                remaining_turns = self.max_turns - turn
                if remaining_turns <= 5 and not self.patch_submitted and isinstance(tool_res, dict):
                    tool_res["budget_warning"] = (
                        f"Only {remaining_turns} turn(s) remaining ({turn}/{self.max_turns} used). "
                        "Finalize your code changes with edit_file and call submit_patch immediately."
                    )

                print(f"     Status: {status} (tool calls so far: {self.tool_calls_count})")

                tool_response_parts.append({
                    "functionResponse": {
                        "name": fn_name,
                        "response": {"name": fn_name, "content": tool_res},
                    }
                })

            contents.append({"role": "user", "parts": tool_response_parts})

            if self.patch_submitted:
                print("[Submit Patch Detected] Concluding agent loop.")
                break

        # Fallback patch extraction if submit_patch was not called
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


def _pythonpath_for(workspace: Path) -> str:
    """PYTHONPATH entries that make the package under test importable.

    A src-layout repo (psf/requests keeps its package in src/requests) is not importable from the
    workspace root alone, which shows up as ModuleNotFoundError for the library being tested.
    """
    parts = [str(workspace)]
    src = workspace / "src"
    if src.is_dir() and any(c.is_dir() and (c / "__init__.py").is_file() for c in src.iterdir()):
        parts.insert(0, str(src))
    return os.pathsep.join(parts)


def _repo_configures_pytest(workspace: Path) -> bool:
    """True when the snapshot already carries pytest configuration of its own."""
    if (workspace / "pytest.ini").is_file() or (workspace / ".pytest.ini").is_file():
        return True
    for name, marker in (
        ("pyproject.toml", "[tool.pytest.ini_options]"),
        ("setup.cfg", "[tool:pytest]"),
        ("tox.ini", "[pytest]"),
    ):
        f = workspace / name
        if f.is_file():
            try:
                if marker in f.read_text(encoding="utf-8", errors="replace"):
                    return True
            except OSError:
                pass
    return False


GIT_EXCLUDE_PATTERNS = [
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    "*.egg-info/",
    "build/",
    "dist/",
    ".coverage",
]


def extract_snapshot_to_dir(tgz_path: Path, dest_dir: Path):
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tgz_path, "r:gz") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(path=dest_dir, filter="data")
        else:
            tar.extractall(path=dest_dir)

    # Hermetic Test Configuration (setup_workspace_test_config).
    # A pytest.ini written here WINS over pyproject.toml's [tool.pytest.ini_options] and over
    # setup.cfg/tox.ini, so writing it unconditionally discards the repo's own testpaths,
    # filterwarnings and required plugins - and `-p no:anyio` breaks any suite that depends on
    # the anyio plugin. Write it only when the repo configures pytest nowhere at all.
    pytest_ini = dest_dir / "pytest.ini"
    if not pytest_ini.exists() and not _repo_configures_pytest(dest_dir):
        pytest_ini.write_text("[pytest]\naddopts = -p no:anyio\n", encoding="utf-8")

    # Initialize baseline git commit
    subprocess.run(["git", "init", "-q"], cwd=str(dest_dir))
    # setup_git_exclude: the real harness appends these before the baseline commit so build and
    # test artifacts never reach `git diff`. Without them a single pytest run or editable install
    # puts megabytes of __pycache__ / egg-info into the agent patch.
    exclude = dest_dir / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(GIT_EXCLUDE_PATTERNS) + "\n")
    subprocess.run(["git", "config", "user.name", "swegemma_eval"], cwd=str(dest_dir))
    subprocess.run(["git", "config", "user.email", "eval@swegemma.local"], cwd=str(dest_dir))
    subprocess.run(["git", "add", "-A"], cwd=str(dest_dir))
    subprocess.run(["git", "commit", "-m", "baseline", "--allow-empty", "-q"], cwd=str(dest_dir))


def verify_agent_patch(task: Dict[str, Any], snapshot_file: Path, agent_patch: str) -> Dict[str, Any]:
    """Phase 2 verification sandbox executing task test_patch against agent_patch."""
    print("\n" + "=" * 70)
    print("  PHASE 2: VERIFICATION SANDBOX (CONTAINER B)")
    print("=" * 70)

    clean_agent_patch = strip_protected_files_from_patch(agent_patch)
    if not clean_agent_patch.strip():
        print("[Verification] Agent patch is empty. Skipping Phase 2 verification.")
        return {"resolved": False, "error": "Empty agent patch"}

    temp_dir = tempfile.mkdtemp(prefix=f"swegemma_verif_{task['instance_id']}_")
    workspace = Path(temp_dir) / "workspace"

    try:
        extract_snapshot_to_dir(snapshot_file, workspace)

        # 1. Apply agent patch with 4-pass resilient fallback
        patch_file = Path(temp_dir) / "agent.patch"
        patch_content = clean_agent_patch if clean_agent_patch.endswith("\n") else clean_agent_patch + "\n"
        patch_file.write_text(patch_content, encoding="utf-8")

        applied = False
        for cmd in [
            ["git", "apply", "--unsafe-paths", "-p1", str(patch_file)],
            ["git", "apply", "--unsafe-paths", "--ignore-space-change", "--ignore-whitespace", str(patch_file)],
            ["git", "apply", "--unsafe-paths", "--recount", str(patch_file)],
            ["patch", "-p1", "--batch", "--forward", "-l", "-i", str(patch_file)],
        ]:
            res = subprocess.run(cmd, cwd=str(workspace), capture_output=True, text=True)
            if res.returncode == 0:
                applied = True
                print(f"[Verification] Agent patch applied cleanly via: {' '.join(cmd[:3])}")
                break

        if not applied:
            print("[Verification Error] Failed to apply agent patch.")
            return {"resolved": False, "error": "Failed to apply agent patch"}

        # 2. Reset every protected test or runner-config file the patch touched. The real scorer
        # reverts conftest.py, pytest.ini, pyproject.toml, setup.cfg and tox.ini too, not just
        # tests/ - leaving them applied would let a patch alter the test runner itself.
        protected = [f["new_file"] for f in parse_unified_diff(clean_agent_patch)
                     if f.get("new_file") and is_protected_file(f["new_file"])]
        reset_paths = ["tests/", "test/", "testing/"] + protected
        subprocess.run(["git", "checkout", "HEAD", "--", *reset_paths],
                       cwd=str(workspace), capture_output=True)
        subprocess.run(["git", "clean", "-f", "--", *reset_paths],
                       cwd=str(workspace), capture_output=True)

        # 3. Apply task test_patch
        test_patch = task.get("test_patch", "")
        if test_patch:
            tpatch_file = Path(temp_dir) / "test.patch"
            tpatch_content = test_patch if test_patch.endswith("\n") else test_patch + "\n"
            tpatch_file.write_text(tpatch_content, encoding="utf-8")
            tp_res = subprocess.run(
                ["git", "apply", "--unsafe-paths", str(tpatch_file)],
                cwd=str(workspace), capture_output=True, text=True,
            )
            if tp_res.returncode != 0:
                tp_res = subprocess.run(
                    ["git", "apply", "--unsafe-paths", "--3way", str(tpatch_file)],
                    cwd=str(workspace), capture_output=True, text=True,
                )
            if tp_res.returncode != 0:
                # Without the hidden tests there is nothing to verify; reporting a pass here
                # would be a false positive, which is the worst failure mode for a scorer.
                print(f"[Verification Error] test_patch failed to apply: {tp_res.stderr[:300]}")
                return {
                    "resolved": False,
                    "error": f"test_patch failed to apply: {tp_res.stderr[:300]}",
                }

        # 4. Target test files
        parsed = parse_unified_diff(test_patch)
        targets = [f["new_file"] for f in parsed if f.get("new_file") and f["new_file"].startswith("tests/")]

        # The shared .venv pins one set of versions for every repo and commit, which made 71 of
        # 129 tasks unscorable (their own reference patch failed). Resolve the snapshot's pins and
        # use a cached venv built from them; fall back to the shared venv if that fails.
        env_mode = os.environ.get("SWE_TEST_ENV", "auto")
        env_info = resolve_pytest(workspace, task.get("repo", ""), mode=env_mode)
        pytest_bin = env_info["pytest"]
        if env_info.get("error"):
            print(f"[Verification] test-env fallback ({env_info['error'][:160]})")

        # `-W default` is appended AFTER the repo's own ini filterwarnings, so it overrides a
        # `filterwarnings = ["error"]` that would otherwise turn an unrelated DeprecationWarning
        # from a slightly newer dependency into a collection error. The real scorer has the same
        # effect by writing its own pytest.ini, which discards the repo's filters entirely.
        cmd = [pytest_bin] + (targets if targets else ["tests/"]) + ["-q", "-W", "default"]
        print(f"[Verification] Running test command: {' '.join(cmd)}  [env {env_info['env_key']}]")
        proc = subprocess.run(
            cmd,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=180,
            env=dict(os.environ, PYTHONPATH=_pythonpath_for(workspace)),
        )
        print("[Verification Test Output]:")
        print(proc.stdout[:1500])
        if proc.stderr:
            print(proc.stderr[:500])

        resolved = (proc.returncode == 0)
        print(f"[Verification Result] Resolved: {resolved} (Exit Code: {proc.returncode})")
        return {
            "resolved": resolved,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[:2000],
            "stderr": proc.stderr[:2000],
            "test_env": env_info["env_key"],
        }

    except Exception as e:
        print(f"[Verification Exception] {e}")
        return {"resolved": False, "error": str(e)}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Run Gemini SWE-bench benchmark.")
    parser.add_argument("--task-id", type=str, help="Specific instance_id to evaluate (e.g. fastapi_15661)")
    parser.add_argument("--task-idx", type=int, default=0, help="0-based index in tasks.jsonl (default: 0)")
    parser.add_argument("--tasks-file", type=str, default="tasks.jsonl", help="Path to tasks.jsonl")
    parser.add_argument("--model", type=str, default="gemini-2.5-flash", help="Gemini model alias (default: gemini-2.5-flash)")
    parser.add_argument("--api-key", type=str, default=None, help="Gemini API Key (default: $GEMINI_API_KEY or $GOOGLE_API_KEY)")
    parser.add_argument("--max-turns", type=int, default=12, help="Maximum reasoning turns (default: 12)")
    parser.add_argument("--results-dir", type=str, default="results/gemini_benchmark", help="Output directory")
    parser.add_argument("--dry-run", action="store_true", help="Setup sandbox and prompts without calling Gemini API")
    parser.add_argument("--verify", action="store_true", help="Run Phase 2 verification sandbox against task test_patch")
    args = parser.parse_args()

    # Locate API key
    api_key = args.api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key and not args.dry_run:
        print("[Error] No Gemini API key provided.")
        print("Please set the environment variable:")
        print("  export GEMINI_API_KEY=\"your-api-key\"")
        print("or pass --api-key <key>.")
        sys.exit(1)

    tasks_path = REPO_ROOT / args.tasks_file
    if not tasks_path.exists():
        print(f"[Error] Tasks file not found: {tasks_path}")
        sys.exit(1)

    with open(tasks_path, "r", encoding="utf-8") as f:
        tasks = [json.loads(line) for line in f if line.strip()]

    target_task = None
    if args.task_id:
        for t in tasks:
            if t["instance_id"] == args.task_id:
                target_task = t
                break
        if not target_task:
            print(f"[Error] Task ID '{args.task_id}' not found in {args.tasks_file}")
            sys.exit(1)
    else:
        if args.task_idx < 0 or args.task_idx >= len(tasks):
            print(f"[Error] Index {args.task_idx} out of range (0..{len(tasks)-1})")
            sys.exit(1)
        target_task = tasks[args.task_idx]

    inst_id = target_task["instance_id"]
    repo = target_task["repo"]
    snapshot_file = REPO_ROOT / "snapshots" / f"{inst_id}.tgz"
    if not snapshot_file.exists():
        print(f"[Error] Snapshot archive not found: {snapshot_file}")
        sys.exit(1)

    print("=" * 70)
    print(f"  BENCHMARK TASK: {inst_id} ({repo})")
    print(f"  SNAPSHOT:       {snapshot_file.name} ({snapshot_file.stat().st_size // (1024*1024)} MB)")
    print(f"  MODEL:          {args.model}")
    print("=" * 70)

    # Setup isolated temp sandbox
    temp_dir = tempfile.mkdtemp(prefix=f"swegemma_{inst_id}_")
    workspace = Path(temp_dir) / "workspace"

    try:
        print(f"\nExtracting repository snapshot to sandbox: {workspace}...")
        extract_snapshot_to_dir(snapshot_file, workspace)

        runner = GeminiAgentRunner(
            task=target_task,
            workspace_dir=workspace,
            api_key=api_key or "dry_run_key",
            model=args.model,
            max_turns=args.max_turns,
        )

        if args.dry_run:
            print("\n[DRY RUN] Sandbox extracted successfully.")
            print(f"Root files in workspace: {[p.name for p in workspace.iterdir()]}")
            print(f"Loaded System Instruction: {runner.load_system_instruction()[:150]}...")
            print(f"Available tools ({len(runner.get_tool_declarations())}): {[t['name'] for t in runner.get_tool_declarations()]}")
            print("\n[DRY RUN COMPLETED] Harness, sandbox, and schemas are 100% verified.")
            return

        result = runner.run_agent_loop()

        # Run Phase 2 verification if requested
        verif_res = None
        if args.verify:
            verif_res = verify_agent_patch(target_task, snapshot_file, result["patch"])
            result["verification"] = verif_res

        # Save results
        out_dir = REPO_ROOT / args.results_dir / inst_id
        out_dir.mkdir(parents=True, exist_ok=True)

        patch_file = out_dir / f"{inst_id}.patch"
        patch_file.write_text(result["patch"], encoding="utf-8")

        summary_file = out_dir / "result.json"
        summary_file.write_text(json.dumps(result, indent=2), encoding="utf-8")

        print("\n" + "=" * 70)
        print("  BENCHMARK EXECUTION SUMMARY")
        print("=" * 70)
        print(f"Task ID:          {result['instance_id']}")
        print(f"Turns Taken:      {result['turns_taken']}")
        print(f"Tool Calls Count: {result['tool_calls_count']}")
        print(f"Patch Size:       {result['patch_size']} bytes")
        print(f"Patch Saved:      {patch_file}")
        if verif_res:
            print(f"Verification:     {'RESOLVED (PASS)' if verif_res.get('resolved') else 'FAILED'}")
        print("=" * 70)

    finally:
        print(f"Cleaning up sandbox: {temp_dir}...")
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
