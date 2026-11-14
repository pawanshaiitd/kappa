#!/usr/bin/env python3
"""Faithful local test harness for a swegemma submission directory.

Unlike scripts/interactive_harness.py (which only reads prompts/system.md and a flat
tool_schemas.json), this script *compiles the submission the way the scorer does*:

  * resolves agent.yaml + !include (.md/.txt raw, .yaml recursive, no abs paths, no "..")
  * enforces the single-declared-model rule and the allowed-model list
  * exposes ONLY the tools each agent declares under `tools:`
  * turns every `agent_tool: {config_path: ...}` into a callable tool that runs the
    sub-agent as its own conversation with its own instruction and its own tools
  * loads `skills:` the way ADK does (kebab-case name == directory name, references/)
    and exposes load_skill / load_skill_resource
  * applies generate_content_config (temperature/top_p/top_k/max_output_tokens/thinking)
  * applies eval_config.yaml budgets, budget_warning injection, free submit_patch/get_status
  * builds the same initial user prompt (problem, hints, budget, env rules, code
    intelligence, workspace layout) and the same 3-nudge continuation protocol
  * extracts the patch with `git add -N . && git diff` + protected-file stripping,
    including the automatic fallback when the agent never calls submit_patch

Backends:
  --backend gemini   Google Generative Language REST  (GEMINI_API_KEY / GOOGLE_API_KEY)
  --backend openai   any OpenAI-compatible /v1 endpoint: real Gemma 4 under vLLM, or a
                     LiteLLM proxy. Use this for the closest thing to the real scorer.

Usage:
    # validate only, no API calls
    python scripts/submission_harness.py --dry-run --task-id fastapi_15589

    # autonomous run, then Phase 2 verification
    python scripts/submission_harness.py --task-id fastapi_15589 --auto --verify

    # interactive REPL (type instructions, /diff, /run, /submit ...)
    python scripts/submission_harness.py --task-id fastapi_15589

    # against local vLLM serving the real competition model
    python scripts/submission_harness.py --task-id fastapi_15589 --auto \
        --backend openai --api-base http://127.0.0.1:8000/v1 \
        --model gemma-4-31b-it-qat-w4a16-ct
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from scripts.harness_tools import (  # noqa: E402
    CodeEmbeddingIndex,
    CodeGraph,
    FileEditError,
    apply_replacement_3tier,
    parse_unified_diff,
    strip_protected_files_from_patch,
)
from scripts.run_gemini_benchmark import (  # noqa: E402
    extract_snapshot_to_dir,
    verify_agent_patch,
)

ALLOWED_MODEL_NAMES = {
    "gemma-4-31b-it-qat-w4a16-ct",
    "gemma-4-12b-it-qat-w4a16-ct",
    "gemma-4-e4b-it-qat-w4a16-ct",
    "gemma-4-e2b-it-qat-w4a16-ct",
}

C_GREEN, C_RED, C_CYAN = "\033[92m", "\033[91m", "\033[96m"
C_YELLOW, C_BOLD, C_DIM, C_RESET = "\033[93m", "\033[1m", "\033[2m", "\033[0m"


def color_diff(text: str) -> str:
    out = []
    for line in text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            out.append(f"{C_BOLD}{line}{C_RESET}")
        elif line.startswith("+"):
            out.append(f"{C_GREEN}{line}{C_RESET}")
        elif line.startswith("-"):
            out.append(f"{C_RED}{line}{C_RESET}")
        elif line.startswith("@@"):
            out.append(f"{C_CYAN}{line}{C_RESET}")
        else:
            out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# Submission compilation
# --------------------------------------------------------------------------------------

class SubmissionError(Exception):
    pass


def _include_loader(root: Path, base_dir: Path, depth: int = 0):
    """yaml.SafeLoader subclass implementing the sandboxed !include directive."""
    if depth > 10:
        raise SubmissionError("!include depth exceeded 10 (cycle?)")

    class Loader(yaml.SafeLoader):
        pass

    def construct_include(loader: yaml.Loader, node: yaml.Node) -> Any:
        rel = str(loader.construct_scalar(node))
        if rel.startswith("/") or "\x00" in rel or ".." in Path(rel).parts:
            raise SubmissionError(f"!include path rejected (absolute/traversal): {rel}")
        target = (base_dir / rel).resolve()
        if not str(target).startswith(str(root.resolve())):
            raise SubmissionError(f"!include escapes submission root: {rel}")
        if not target.exists():
            raise SubmissionError(f"!include target not found: {rel}")
        if target.is_symlink():
            raise SubmissionError(f"!include target is a symlink: {rel}")
        if target.suffix in (".md", ".txt"):
            return target.read_text(encoding="utf-8")
        if target.suffix in (".yaml", ".yml"):
            return yaml.load(
                target.read_text(encoding="utf-8"),
                _include_loader(root, target.parent, depth + 1),
            )
        raise SubmissionError(f"!include unsupported extension: {rel}")

    Loader.add_constructor("!include", construct_include)
    return Loader


def load_yaml_with_includes(path: Path, root: Path) -> Dict[str, Any]:
    return yaml.load(path.read_text(encoding="utf-8"), _include_loader(root, path.parent))


def parse_skill_md(skill_dir: Path) -> Dict[str, Any]:
    """Minimal re-implementation of ADK's skill loader, with the same validation."""
    md = skill_dir / "SKILL.md"
    if not md.exists():
        raise SubmissionError(f"skill {skill_dir} has no SKILL.md")
    text = md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise SubmissionError(f"skill {skill_dir}: SKILL.md has no YAML frontmatter")
    _, fm_raw, body = text.split("---", 2)
    fm = yaml.safe_load(fm_raw) or {}
    name = fm.get("name")
    if not name:
        raise SubmissionError(f"skill {skill_dir}: frontmatter has no name")
    import re

    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", str(name)):
        raise SubmissionError(
            f"skill {skill_dir}: name {name!r} must be lowercase kebab-case "
            "(ADK rejects snake_case unless SNAKE_CASE_SKILL_NAME is enabled)"
        )
    if skill_dir.name != name:
        raise SubmissionError(
            f"skill directory {skill_dir.name!r} must equal frontmatter name {name!r}"
        )
    if not fm.get("description"):
        raise SubmissionError(f"skill {name}: frontmatter has no description")
    refs = {}
    for sub in ("references", "assets"):
        d = skill_dir / sub
        if d.is_dir():
            for f in sorted(d.rglob("*")):
                if f.is_file():
                    refs[f"{sub}/{f.relative_to(d)}"] = f.read_text(
                        encoding="utf-8", errors="replace"
                    )
    if (skill_dir / "resources").is_dir():
        print(
            f"{C_YELLOW}[warn] skill {name}: 'resources/' is ignored by ADK; "
            f"rename it to 'references/'{C_RESET}"
        )
    return {
        "name": name,
        "description": fm["description"],
        "instructions": body.strip(),
        "resources": refs,
    }


class AgentSpec:
    def __init__(self, cfg: Dict[str, Any], root: Path, source: Path):
        self.source = source
        self.name: str = cfg.get("name") or source.stem
        self.description: str = cfg.get("description", "")
        self.model: Optional[str] = cfg.get("model")
        self.instruction: str = cfg.get("instruction", "") or ""
        self.gen_cfg: Dict[str, Any] = cfg.get("generate_content_config") or {}
        self.tool_names: List[str] = []
        self.agent_tools: List["AgentSpec"] = []
        self.skills: List[Dict[str, Any]] = []

        for entry in cfg.get("tools") or []:
            if isinstance(entry, str):
                self.tool_names.append(entry)
            elif isinstance(entry, dict) and "agent_tool" in entry:
                cp = entry["agent_tool"].get("config_path")
                if not cp:
                    raise SubmissionError(f"{source}: agent_tool without config_path")
                sub_path = (source.parent / cp).resolve()
                if not str(sub_path).startswith(str(root.resolve())):
                    raise SubmissionError(f"{source}: agent_tool escapes root: {cp}")
                if not sub_path.exists():
                    raise SubmissionError(f"{source}: agent_tool not found: {cp}")
                self.agent_tools.append(
                    AgentSpec(load_yaml_with_includes(sub_path, root), root, sub_path)
                )
            else:
                raise SubmissionError(f"{source}: unsupported tools entry: {entry!r}")

        for rel in cfg.get("skills") or []:
            sd = (root / rel).resolve()
            if not str(sd).startswith(str(root.resolve())) or not sd.is_dir():
                raise SubmissionError(f"{source}: skill path invalid: {rel}")
            self.skills.append(parse_skill_md(sd))

    def all_agents(self) -> List["AgentSpec"]:
        out = [self]
        for a in self.agent_tools:
            out.extend(a.all_agents())
        return out


class Submission:
    def __init__(self, directory: Path):
        self.dir = directory.resolve()
        roots = [
            p
            for n in ("agent.yaml", "agent.yml", "root_agent.yaml", "root_agent.yml")
            for p in [self.dir / n]
            if p.exists()
        ]
        if not roots:
            raise SubmissionError(f"no root config in {self.dir} (MissingRootConfigError)")
        if len(roots) > 1:
            raise SubmissionError(f"multiple root configs: {[p.name for p in roots]}")
        self.root_path = roots[0]
        self.root = AgentSpec(
            load_yaml_with_includes(self.root_path, self.dir), self.dir, self.root_path
        )

        self.budget: Dict[str, Any] = {
            "timeout_seconds": 300,
            "max_tool_calls": 100,
            "max_time_minutes": 60.0,
            "max_turns": 500,
        }
        ec = self.dir / "eval_config.yaml"
        if ec.exists():
            data = yaml.safe_load(ec.read_text(encoding="utf-8")) or {}
            self.budget.update((data.get("evaluation") or {}))

    def validate(self) -> List[str]:
        """Returns the list of problems a scorer would reject or penalise."""
        problems: List[str] = []
        agents = self.root.all_agents()

        models = {a.model for a in agents if a.model}
        for a in agents:
            if not a.model:
                problems.append(f"agent {a.name} ({a.source.name}) declares no model")
        if len(models) > 1:
            problems.append(f"multiple base models declared: {sorted(models)}")
        for m in models:
            if m.split("/")[-1] not in ALLOWED_MODEL_NAMES:
                problems.append(f"model {m!r} is not in ALLOWED_MODEL_NAMES")

        for a in agents:
            if not a.instruction.strip():
                problems.append(f"agent {a.name} has an empty instruction")
            g = a.gen_cfg or {}
            if g.get("max_output_tokens", 0) > 32768:
                problems.append(f"agent {a.name}: max_output_tokens > 32768")
            tb = (g.get("thinking_config") or {}).get("thinking_budget", 0)
            if tb and tb > 32768:
                problems.append(f"agent {a.name}: thinking_budget > 32768")
            if (g.get("thinking_config") or {}).get("thinking_level"):
                problems.append(
                    f"agent {a.name}: thinking_level maps to reasoning_effort and is "
                    "unreliable on vLLM; use thinking_budget instead"
                )
            for t in a.tool_names:
                if t not in TOOL_SCHEMAS:
                    problems.append(f"agent {a.name}: unknown tool {t!r}")

        allowed_ext = {".yaml", ".yml", ".md", ".txt", ".py", ".json", ".safetensors"}
        total = 0
        for f in self.dir.rglob("*"):
            if f.is_symlink():
                problems.append(f"symlink not allowed: {f.relative_to(self.dir)}")
            if f.is_file():
                total += f.stat().st_size
                if f.suffix.lower() not in allowed_ext:
                    problems.append(f"disallowed file extension: {f.relative_to(self.dir)}")
        if total > 3 * 1024**3:
            problems.append(f"unpacked size {total/1024**3:.2f} GiB exceeds 3 GiB")

        adapters = self.dir / "adapters"
        if adapters.is_dir():
            real = [d for d in adapters.iterdir() if d.is_dir()]
            for d in real:
                if not (d / "adapter_model.safetensors").exists():
                    problems.append(f"adapters/{d.name} has no adapter_model.safetensors")
            if real:
                problems.append(
                    f"adapters/ present ({[d.name for d in real]}): this switches LoRA on in "
                    "vLLM. Ship it only if the adapter is trained and vLLM-tested."
                )
        return problems

    def describe(self) -> str:
        lines = [f"root: {self.root_path.name}"]

        def walk(a: AgentSpec, indent: str):
            lines.append(
                f"{indent}{a.name}  [{a.model}]  instruction={len(a.instruction)} chars"
            )
            if a.tool_names:
                lines.append(f"{indent}  tools: {', '.join(a.tool_names)}")
            if a.skills:
                lines.append(f"{indent}  skills: {', '.join(s['name'] for s in a.skills)}")
            if a.gen_cfg:
                lines.append(f"{indent}  gen: {json.dumps(a.gen_cfg, sort_keys=True)}")
            for sub in a.agent_tools:
                walk(sub, indent + "    ")

        walk(self.root, "  ")
        lines.append(f"budget: {json.dumps(self.budget, sort_keys=True)}")
        return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Tool schemas (descriptions mirror HARNESS_README.md section 6)
# --------------------------------------------------------------------------------------

TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "run_command": {
        "description": (
            "Execute a shell command with bash -c inside /workspace. stdout and stderr are "
            "each truncated to 5000 characters. Returns status, stdout, stderr, exit_code."
        ),
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Shell command"}},
            "required": ["command"],
        },
    },
    "read_file": {
        "description": (
            "Read a file under /workspace with 1-indexed inclusive line slicing. Returns at "
            "most 150 lines and 10000 characters per call; check is_truncated and total_lines "
            "and page with further calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filepath": {"type": "string", "description": "Path relative to /workspace"},
                "start_line": {"type": "integer", "description": "1-based first line"},
                "end_line": {"type": "integer", "description": "1-based last line"},
            },
            "required": ["filepath"],
        },
    },
    "edit_file": {
        "description": (
            "Replace old_string with new_string in an existing file under /workspace, using "
            "exact then whitespace-flexible then regex matching. old_string must match exactly "
            "one place unless allow_multiple is true; copy it verbatim from a recent read_file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filepath": {"type": "string"},
                "old_string": {"type": "string", "description": "Verbatim text to replace"},
                "new_string": {"type": "string", "description": "Replacement text"},
                "allow_multiple": {"type": "boolean", "description": "Allow >1 occurrence"},
            },
            "required": ["filepath", "old_string", "new_string"],
        },
    },
    "write_file": {
        "description": (
            "Create or overwrite /workspace/<filepath>, creating parent directories. Files you "
            "create inside /workspace end up in your patch; put scratch files in /tmp instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {"filepath": {"type": "string"}, "content": {"type": "string"}},
            "required": ["filepath", "content"],
        },
    },
    "submit_patch": {
        "description": (
            "Capture the current git diff as the final patch and end the session. Free: does "
            "not count against the tool-call budget. Call it last, after verifying."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    "get_status": {
        "description": (
            "Report remaining tool calls, remaining time and patch status. Free: does not count "
            "against the tool-call budget."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    "search_similar_code": {
        "description": (
            "Find the k symbols most similar to a dotted symbol path (pkg.module.Class.method, "
            "Class.method, or a bare function name) in the precomputed embedding index."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Dotted symbol path or symbol name"},
                "k": {"type": "integer", "description": "How many results (default 10)"},
            },
            "required": ["query"],
        },
    },
    "get_code_neighbors": {
        "description": (
            "List callers and callees of a symbol in the precomputed call graph. node is a "
            "dotted symbol path; edge_type optionally filters (e.g. 'calls')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Dotted symbol path"},
                "edge_type": {"type": "string", "description": "Optional edge filter"},
                "max_neighbors": {"type": "integer"},
            },
            "required": ["node"],
        },
    },
    "get_code_subgraph": {
        "description": "Return the induced call subgraph connecting a list of dotted symbol paths.",
        "parameters": {
            "type": "object",
            "properties": {
                "nodes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["nodes"],
        },
    },
    "load_skill": {
        "description": "Load the full instructions of one of the available skills by name.",
        "parameters": {
            "type": "object",
            "properties": {"skill_name": {"type": "string"}},
            "required": ["skill_name"],
        },
    },
    "load_skill_resource": {
        "description": "Load one reference file belonging to a skill, e.g. references/guide.md.",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string"},
                "file_path": {"type": "string"},
            },
            "required": ["skill_name", "file_path"],
        },
    },
}

FREE_TOOLS = {"submit_patch", "get_status"}


# --------------------------------------------------------------------------------------
# Sandbox + tool implementations
# --------------------------------------------------------------------------------------

class Sandbox:
    def __init__(self, workspace: Path, task: Dict[str, Any], budget: Dict[str, Any]):
        self.ws = workspace.resolve()
        self.task = task
        self.budget = budget
        self.tool_calls_used = 0
        self.tool_uses: "collections.Counter[str]" = collections.Counter()
        self.tool_errors: "collections.Counter[str]" = collections.Counter()
        self.edits_landed = 0
        # Failing calls are retained verbatim: run_command fails 60%+ of the time in practice and
        # the aggregate counters alone do not say which commands the model gets wrong.
        self.failed_calls: List[Dict[str, Any]] = []
        self.patch_submitted = False
        self.submitted_patch = ""
        self.start_time = time.time()
        self.graph: Optional[CodeGraph] = None
        self.emb: Optional[CodeEmbeddingIndex] = None
        self._load_code_intel()

    # -- budget -------------------------------------------------------------------
    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    @property
    def time_remaining(self) -> float:
        return float(self.budget["max_time_minutes"]) * 60.0 - self.elapsed

    @property
    def calls_remaining(self) -> int:
        return int(self.budget["max_tool_calls"]) - self.tool_calls_used

    def budget_exhausted(self) -> Optional[str]:
        if self.time_remaining <= 0:
            return "time budget exhausted"
        if self.calls_remaining <= 0:
            return "tool-call budget exhausted"
        return None

    def _load_code_intel(self) -> None:
        repo = self.task["repo"].replace("/", "_")
        commit = self.task.get("base_commit", "")
        g = REPO_ROOT / "graphs" / f"{repo}_{commit}.json"
        e = REPO_ROOT / "embeddings" / f"{repo}_{commit}.npz"
        if g.exists():
            self.graph = CodeGraph(str(g))
        if e.exists():
            self.emb = CodeEmbeddingIndex(str(e))

    def has_code_intel(self) -> bool:
        return self.graph is not None or self.emb is not None

    # -- helpers ------------------------------------------------------------------
    def _resolve(self, filepath: str) -> Path:
        clean = str(filepath).strip()
        for pref in ("/workspace/", "workspace/"):
            if clean.startswith(pref):
                clean = clean[len(pref) :]
        clean = clean.lstrip("/") or "."
        if ".." in Path(clean).parts:
            raise ValueError(f"path traversal rejected: {filepath}")
        target = (self.ws / clean).resolve()
        if not str(target).startswith(str(self.ws)):
            raise ValueError(f"path escapes workspace: {filepath}")
        return target

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(self.ws), capture_output=True, text=True
        )

    def live_diff(self) -> str:
        self.git("add", "-N", ".")
        return strip_protected_files_from_patch(self.git("diff", "--binary", "HEAD").stdout)

    def workspace_layout(self, limit: int = 200) -> str:
        proc = subprocess.run(
            ["find", ".", "-mindepth", "1", "-maxdepth", "3", "-not", "-path", "*/.*"],
            cwd=str(self.ws),
            capture_output=True,
            text=True,
        )
        skip = ("__pycache__", ".pyc", ".egg-info")
        prune = ("./docs/", "./docs_src/", "./tests/", "./examples/", "./benchmarks/")
        entries = []
        for line in proc.stdout.splitlines():
            if any(s in line for s in skip):
                continue
            if any(line.startswith(p) for p in prune):
                continue
            entries.append(line)
        entries.sort()
        return "\n".join(entries[:limit])

    # -- tool dispatch ------------------------------------------------------------
    def call(self, name: str, args: Dict[str, Any], skills: Dict[str, Any]) -> Dict[str, Any]:
        if name not in FREE_TOOLS:
            blocked = self.budget_exhausted()
            if blocked:
                return {
                    "status": "error",
                    "error_type": "BudgetExhausted",
                    "error_message": f"{blocked}; call submit_patch now",
                }
            self.tool_calls_used += 1
        res = self._dispatch(name, args, skills)
        self.tool_uses[name] += 1
        if res.get("status") == "error":
            self.tool_errors[f"{name}:{res.get('error_type')}"] += 1
            if len(self.failed_calls) < 40:
                detail = res.get("error_message") or ""
                if name == "run_command":
                    detail = (res.get("details", {}) or {}).get("stderr", "") or detail
                self.failed_calls.append(
                    {
                        "tool": name,
                        "error_type": res.get("error_type"),
                        # The argument the model got wrong is the thing worth seeing: the command
                        # string, the path, or the old_string it invented.
                        "arg": str(
                            args.get("command")
                            or args.get("filepath")
                            or args.get("query")
                            or args.get("node")
                            or ""
                        )[:300],
                        "detail": str(detail)[:200],
                    }
                )
        elif name in ("edit_file", "write_file"):
            self.edits_landed += 1
        if (
            name not in FREE_TOOLS
            and int(self.budget["max_tool_calls"]) >= 20
            and self.calls_remaining <= 10
        ):
            res["budget_warning"] = (
                f"Only {self.calls_remaining} tool call(s) remaining "
                f"({self.tool_calls_used}/{self.budget['max_tool_calls']} used). "
                "Finalize your edits and call submit_patch soon."
            )
        return res

    def _dispatch(self, name: str, args: Dict[str, Any], skills: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if name == "read_file":
                fp = args.get("filepath", "")
                target = self._resolve(fp)
                if not target.exists():
                    return _err("FileNotFoundError", f"File not found: {fp}")
                if target.is_dir():
                    return _err("IsADirectoryError", f"Expected file, got directory: {fp}")
                lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
                start = max(1, int(args.get("start_line") or 1))
                end = int(args.get("end_line") or len(lines))
                window = lines[start - 1 : min(end, len(lines))]
                truncated = len(window) > 150
                window = window[:150]
                chunk = "\n".join(window)
                if len(chunk) > 10000:
                    chunk = chunk[:10000]
                    chunk = chunk[: chunk.rfind("\n") + 1] if "\n" in chunk else chunk
                    window = chunk.splitlines()
                    truncated = True
                if start - 1 + len(window) < min(end, len(lines)):
                    truncated = True
                return {
                    "status": "ok",
                    "filepath": fp,
                    "content": chunk,
                    "start_line": start,
                    "end_line": start + len(window) - 1,
                    "total_lines": len(lines),
                    "is_truncated": truncated,
                }

            if name == "edit_file":
                fp = args.get("filepath", "")
                target = self._resolve(fp)
                if not target.exists():
                    return _err("FileEditError", f"File not found: {fp}")
                content = target.read_text(encoding="utf-8", errors="replace")
                if not content:
                    return _err("FileEditError", f"File is empty: {fp}")
                if not args.get("old_string"):
                    return _err("FileEditError", "old_string must not be empty")
                before = content
                updated, strategy, count = apply_replacement_3tier(
                    content,
                    args["old_string"],
                    args.get("new_string", ""),
                    bool(args.get("allow_multiple", False)),
                )
                target.write_text(updated, encoding="utf-8")
                diff = _unified(before, updated, fp)
                return {
                    "status": "ok",
                    "filepath": fp,
                    "occurrences": count,
                    "strategy": strategy,
                    "diff": diff[:5000],
                    "is_truncated": len(diff) > 5000,
                }

            if name == "write_file":
                fp = args.get("filepath", "")
                target = self._resolve(fp)
                target.parent.mkdir(parents=True, exist_ok=True)
                body = args.get("content", "")
                target.write_text(body, encoding="utf-8")
                return {"status": "ok", "filepath": fp, "size": len(body)}

            if name == "run_command":
                cmd = str(args.get("command", "")).replace("/workspace", str(self.ws))
                timeout = min(
                    int(self.budget["timeout_seconds"]), max(5, int(self.time_remaining))
                )
                try:
                    proc = subprocess.run(
                        cmd,
                        shell=True,
                        cwd=str(self.ws),
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                    )
                except subprocess.TimeoutExpired:
                    return _err("TimeoutExceeded", f"Command timed out after {timeout}s")
                if proc.returncode == 0:
                    return {
                        "status": "ok",
                        "stdout": proc.stdout[:5000],
                        "stderr": proc.stderr[:5000],
                        "exit_code": 0,
                    }
                return {
                    "status": "error",
                    "error_type": "CommandError",
                    "error_message": f"Command exited {proc.returncode}",
                    "details": {
                        "stdout": proc.stdout[:5000],
                        "stderr": proc.stderr[:5000],
                        "exit_code": proc.returncode,
                    },
                }

            if name == "submit_patch":
                self.submitted_patch = self.live_diff()
                self.patch_submitted = True
                return {
                    "status": "ok",
                    "patch_size": len(self.submitted_patch),
                    "files_changed": len(parse_unified_diff(self.submitted_patch)),
                }

            if name == "get_status":
                return {
                    "status": "ok",
                    "tool_calls_used": self.tool_calls_used,
                    "tool_calls_remaining": self.calls_remaining,
                    "max_tool_calls": self.budget["max_tool_calls"],
                    "time_seconds_remaining": round(self.time_remaining, 1),
                    "max_time_minutes": self.budget["max_time_minutes"],
                    "agent_elapsed_seconds": round(self.elapsed, 1),
                    "patch_submitted": self.patch_submitted,
                    "patch_size": len(self.live_diff()),
                    "command_timeout_seconds": self.budget["timeout_seconds"],
                }

            if name == "search_similar_code":
                if not self.emb:
                    return _err("NotIndexed", "No embedding index for this repo")
                return self.emb.search_similar_code(
                    str(args.get("query", "")), k=int(args.get("k", 10))
                )

            if name == "get_code_neighbors":
                if not self.graph:
                    return _err("NotIndexed", "No call graph for this repo")
                return self.graph.get_code_neighbors(
                    str(args.get("node", "")),
                    edge_type=args.get("edge_type"),
                    max_neighbors=int(args.get("max_neighbors", 50)),
                )

            if name == "get_code_subgraph":
                if not self.graph:
                    return _err("NotIndexed", "No call graph for this repo")
                nodes = args.get("nodes", [])
                if isinstance(nodes, str):
                    nodes = [n.strip() for n in nodes.split(",") if n.strip()]
                return self.graph.get_code_subgraph(nodes)

            if name == "load_skill":
                sn = args.get("skill_name")
                sn = sn[0] if isinstance(sn, list) and sn else sn
                sk = skills.get(str(sn))
                if not sk:
                    return _err("UnknownSkill", f"No skill named {sn!r}")
                return {
                    "status": "ok",
                    "skill_name": sk["name"],
                    "instructions": sk["instructions"],
                    "resources": sorted(sk["resources"]),
                }

            if name == "load_skill_resource":
                sn = args.get("skill_name")
                sn = sn[0] if isinstance(sn, list) and sn else sn
                fp = args.get("file_path")
                fp = fp[0] if isinstance(fp, list) and fp else fp
                sk = skills.get(str(sn))
                if not sk:
                    return _err("UnknownSkill", f"No skill named {sn!r}")
                body = sk["resources"].get(str(fp))
                if body is None:
                    return _err(
                        "UnknownResource",
                        f"{fp!r} not in {sorted(sk['resources'])}",
                    )
                return {"status": "ok", "file_path": fp, "content": body}

            return _err("UnknownTool", f"Unknown tool: {name}")

        except FileEditError as e:
            return _err("FileEditError", str(e))
        except ValueError as e:
            return _err("ValidationError", str(e))
        except Exception as e:  # noqa: BLE001 - mirror ToolErrorPlugin
            return _err(type(e).__name__, str(e))


def _err(kind: str, msg: str) -> Dict[str, Any]:
    return {"status": "error", "error_type": kind, "error_message": msg}


def _unified(before: str, after: str, path: str) -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


# --------------------------------------------------------------------------------------
# LLM backends
# --------------------------------------------------------------------------------------

class Backend:
    """Canonical message list -> provider payload. Messages are:
    {"role": "user"|"assistant"|"tool", "text": str, "tool_calls": [...], "tool_call_id": str}
    """

    def __init__(self, model: str, api_key: str = "", api_base: str = "", timeout: int = 300):
        self.model = model
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    def _post(self, url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
        last: Optional[Exception] = None
        for attempt in range(5):
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json", **headers},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                if e.code == 403:
                    hint = ""
                    if "IP address restriction" in body or "API_KEY_IP_ADDRESS_BLOCKED" in body:
                        hint = (
                            "\n\nYour API key has an IP address restriction and this machine's "
                            "address is not allowed.\n"
                            "  1. Open https://aistudio.google.com/apikey (or Google Cloud "
                            "Console > Credentials).\n"
                            "  2. Set the key's application restriction to 'None', or add this "
                            "machine's current IP.\n"
                            "  3. Meanwhile you can still run --dry-run, --backend mock, and "
                            "--verify-patch, which make no API calls."
                        )
                    raise RuntimeError(f"HTTP 403: {body[:600]}{hint}") from e
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    delay = 2.0 * (2**attempt)
                    print(f"{C_YELLOW}[retry {attempt+1}/4] HTTP {e.code}; {delay:.0f}s{C_RESET}")
                    time.sleep(delay)
                    last = e
                    continue
                raise RuntimeError(f"HTTP {e.code}: {body[:600]}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < 4:
                    delay = 2.0 * (2**attempt)
                    print(f"{C_YELLOW}[retry {attempt+1}/4] {e}; {delay:.0f}s{C_RESET}")
                    time.sleep(delay)
                    last = e
                    continue
                raise
        raise RuntimeError(f"request failed after retries: {last}")

    def generate(self, system: str, messages: List[Dict[str, Any]],
                 tools: Dict[str, Dict[str, Any]], gen_cfg: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class GeminiBackend(Backend):
    def generate(self, system, messages, tools, gen_cfg):
        contents = []
        for m in messages:
            if m["role"] == "tool":
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": m["name"],
                                    "response": {"content": m["content"]},
                                }
                            }
                        ],
                    }
                )
                continue
            parts: List[Dict[str, Any]] = []
            if m.get("text"):
                parts.append({"text": m["text"]})
            for tc in m.get("tool_calls") or []:
                parts.append({"functionCall": {"name": tc["name"], "args": tc["args"]}})
            if not parts:
                parts = [{"text": ""}]
            contents.append({"role": "model" if m["role"] == "assistant" else "user", "parts": parts})

        cfg: Dict[str, Any] = {}
        if "temperature" in gen_cfg:
            cfg["temperature"] = gen_cfg["temperature"]
        if "top_p" in gen_cfg:
            cfg["topP"] = gen_cfg["top_p"]
        if "top_k" in gen_cfg:
            cfg["topK"] = gen_cfg["top_k"]
        cfg["maxOutputTokens"] = gen_cfg.get("max_output_tokens", 6144)
        tcfg = gen_cfg.get("thinking_config") or {}
        if "2.5" in self.model and tcfg.get("thinking_budget") is not None:
            cfg["thinkingConfig"] = {"thinkingBudget": int(tcfg["thinking_budget"])}

        payload: Dict[str, Any] = {"contents": contents, "generationConfig": cfg}
        if system:
            payload["system_instruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = [
                {
                    "function_declarations": [
                        {"name": n, "description": s["description"], "parameters": s["parameters"]}
                        for n, s in tools.items()
                    ]
                }
            ]

        data = self._post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}",
            payload,
            {},
        )
        cand = (data.get("candidates") or [{}])[0]
        text_parts, calls = [], []
        for p in (cand.get("content") or {}).get("parts") or []:
            if "text" in p:
                text_parts.append(p["text"])
            elif "functionCall" in p:
                fc = p["functionCall"]
                calls.append({"id": fc.get("name", ""), "name": fc.get("name"), "args": fc.get("args") or {}})
        return {
            "text": "\n".join(t for t in text_parts if t.strip()),
            "tool_calls": calls,
            "finish_reason": cand.get("finishReason", ""),
            "usage": data.get("usageMetadata", {}),
        }


class OpenAIBackend(Backend):
    """Any OpenAI-compatible /v1 endpoint: vLLM serving real Gemma 4, or LiteLLM."""

    def generate(self, system, messages, tools, gen_cfg):
        msgs: List[Dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        for m in messages:
            if m["role"] == "tool":
                msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": m.get("tool_call_id") or m["name"],
                        "content": json.dumps(m["content"]),
                    }
                )
                continue
            if m["role"] == "assistant":
                entry: Dict[str, Any] = {"role": "assistant", "content": m.get("text") or ""}
                if m.get("tool_calls"):
                    entry["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])},
                        }
                        for tc in m["tool_calls"]
                    ]
                msgs.append(entry)
            else:
                msgs.append({"role": "user", "content": m.get("text") or ""})

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "max_tokens": gen_cfg.get("max_output_tokens", 6144),
            "temperature": gen_cfg.get("temperature", 0.1),
        }
        if "top_p" in gen_cfg:
            payload["top_p"] = gen_cfg["top_p"]
        if "seed" in gen_cfg:
            payload["seed"] = gen_cfg["seed"]
        tcfg = gen_cfg.get("thinking_config") or {}
        if tcfg.get("thinking_budget"):
            payload["extra_body"] = {
                "enable_thinking": True,
                "thinking_token_budget": int(tcfg["thinking_budget"]),
            }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": n,
                        "description": s["description"],
                        "parameters": s["parameters"],
                    },
                }
                for n, s in tools.items()
            ]
            payload["tool_choice"] = "auto"

        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = self._post(f"{self.api_base}/chat/completions", payload, headers)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            calls.append({"id": tc.get("id") or fn.get("name"), "name": fn.get("name"), "args": args})
        return {
            "text": msg.get("content") or "",
            "tool_calls": calls,
            "finish_reason": choice.get("finish_reason", ""),
            "usage": data.get("usage", {}),
        }


class MockBackend(Backend):
    """No network. Replays a scripted tool sequence so the plumbing can be smoke-tested:
    budget accounting, truncation flags, nudges, patch extraction, Phase 2 verification.

    Pass --mock-script <file.json> with a list of
    {"text": str, "tool_calls": [{"name": ..., "args": {...}}]} turns.
    """

    def __init__(self, script: List[Dict[str, Any]]):
        super().__init__("mock")
        self.script = script
        self.i = 0

    def generate(self, system, messages, tools, gen_cfg):
        if self.i >= len(self.script):
            return {"text": "Script exhausted.", "tool_calls": [], "finish_reason": "STOP", "usage": {}}
        turn = self.script[self.i]
        self.i += 1
        calls = [
            {"id": f"call_{self.i}_{j}", "name": c["name"], "args": c.get("args", {})}
            for j, c in enumerate(turn.get("tool_calls") or [])
        ]
        unknown = [c["name"] for c in calls if c["name"] not in tools]
        if unknown:
            print(f"{C_YELLOW}[mock] script calls undeclared tool(s): {unknown}{C_RESET}")
        return {
            "text": turn.get("text", ""),
            "tool_calls": calls,
            "finish_reason": turn.get("finish_reason", "STOP"),
            "usage": {},
        }


DEFAULT_MOCK_SCRIPT: List[Dict[str, Any]] = [
    {"text": "Checking budget and locating the code.",
     "tool_calls": [{"name": "get_status", "args": {}}]},
    {"text": "Grepping the package for the symbol.",
     "tool_calls": [{"name": "run_command",
                     "args": {"command": "ls && git log --oneline -1"}}]},
    {"text": "Reading a file region.",
     "tool_calls": [{"name": "read_file", "args": {"filepath": "README.md", "start_line": 1,
                                                   "end_line": 400}}]},
    {"text": "Deliberately bad edit to exercise FileEditError recovery.",
     "tool_calls": [{"name": "edit_file", "args": {"filepath": "README.md",
                                                   "old_string": "THIS TEXT DOES NOT EXIST ANYWHERE",
                                                   "new_string": "x"}}]},
    {"text": "A text-only turn, to exercise the nudge protocol.", "tool_calls": []},
    {"text": "Submitting.", "tool_calls": [{"name": "submit_patch", "args": {}}]},
]


# --------------------------------------------------------------------------------------
# Agent execution
# --------------------------------------------------------------------------------------

NUDGE_TRUNCATED = (
    "Your previous response reached the token limit before the tool call finished closing. "
    "Do NOT repeat your prior reasoning - emit your next tool call immediately, and if calling "
    "edit_file or write_file, split the change into smaller incremental edits."
)
NUDGE_MAXTOK = (
    "Your previous response reached the token limit while thinking before a tool call was "
    "completed. Keep reasoning under a few sentences and emit your next tool call immediately, "
    "or call submit_patch when you have completed and verified your changes."
)
NUDGE_NORMAL = (
    "Please continue your work using the available tools, or call submit_patch when you have "
    "completed and verified your changes."
)


class AgentSession:
    """One agent (root or sub-agent) with its own history, tools and sampling config."""

    def __init__(self, spec: AgentSpec, sandbox: Sandbox, backend: Backend,
                 verbose: bool = True, depth: int = 0):
        self.spec = spec
        self.sb = sandbox
        self.backend = backend
        self.verbose = verbose
        self.depth = depth
        self.messages: List[Dict[str, Any]] = []
        self.skills = {s["name"]: s for s in spec.skills}
        self.tools: Dict[str, Dict[str, Any]] = {}
        for t in spec.tool_names:
            if t in TOOL_SCHEMAS:
                self.tools[t] = TOOL_SCHEMAS[t]
        if self.skills:
            self.tools["load_skill"] = TOOL_SCHEMAS["load_skill"]
            self.tools["load_skill_resource"] = TOOL_SCHEMAS["load_skill_resource"]
        for sub in spec.agent_tools:
            self.tools[sub.name] = {
                "description": sub.description
                or f"Delegate to the {sub.name} sub-agent. It sees only the request text you send.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "request": {
                            "type": "string",
                            "description": (
                                "Self-contained instructions. The sub-agent cannot see the "
                                "problem statement or your findings, so include file paths, "
                                "line numbers and exactly what you want returned."
                            ),
                        }
                    },
                    "required": ["request"],
                },
            }
        self.sub_specs = {s.name: s for s in spec.agent_tools}

    def system_instruction(self) -> str:
        parts = [self.spec.instruction]
        if self.skills:
            lines = ["", "## Available Skills", "Call load_skill(skill_name) to read one in full."]
            for s in self.skills.values():
                lines.append(f"- `{s['name']}`: {s['description']}")
            parts.append("\n".join(lines))
        return "\n".join(parts)

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(("    " * self.depth) + msg)

    def run_turn(self) -> Dict[str, Any]:
        """One provider round trip plus any tool calls it requests."""
        t0 = time.perf_counter()
        resp = self.backend.generate(
            self.system_instruction(), self.messages, self.tools, self.spec.gen_cfg
        )
        latency = time.perf_counter() - t0
        self.messages.append(
            {"role": "assistant", "text": resp["text"], "tool_calls": resp["tool_calls"]}
        )
        if resp["text"].strip():
            self._log(f"\n{C_BOLD}{self.spec.name} ({latency:.1f}s):{C_RESET}")
            self._log(resp["text"].strip()[:4000])

        for tc in resp["tool_calls"]:
            name, args = tc["name"], tc["args"]
            preview = json.dumps(args)[:110]
            self._log(f"{C_YELLOW}-> {C_BOLD}{name}{C_RESET}({preview})")

            if name in self.sub_specs:
                result = self._run_sub_agent(self.sub_specs[name], str(args.get("request", "")))
            else:
                result = self.sb.call(name, args, self.skills)

            if result.get("status") == "ok":
                detail = ""
                if name == "run_command":
                    detail = f" exit={result.get('exit_code')}"
                elif name == "edit_file":
                    detail = f" strategy={result.get('strategy')} occ={result.get('occurrences')}"
                elif name == "read_file":
                    detail = (
                        f" lines {result.get('start_line')}-{result.get('end_line')}"
                        f"/{result.get('total_lines')}"
                        f"{' TRUNCATED' if result.get('is_truncated') else ''}"
                    )
                elif name == "submit_patch":
                    detail = f" patch={result.get('patch_size')}B files={result.get('files_changed')}"
                self._log(f"   {C_GREEN}ok{C_RESET}{detail}")
            else:
                self._log(
                    f"   {C_RED}error {result.get('error_type')}: "
                    f"{str(result.get('error_message'))[:200]}{C_RESET}"
                )
            if result.get("budget_warning"):
                self._log(f"   {C_YELLOW}{result['budget_warning']}{C_RESET}")

            self.messages.append(
                {"role": "tool", "name": name, "tool_call_id": tc["id"], "content": result}
            )

        return {
            "had_tool_call": bool(resp["tool_calls"]),
            "text": resp["text"],
            "finish_reason": resp.get("finish_reason", ""),
            "usage": resp.get("usage", {}),
        }

    def _run_sub_agent(self, spec: AgentSpec, request: str) -> Dict[str, Any]:
        self._log(f"   {C_CYAN}[delegating to {spec.name}]{C_RESET}")
        sub = AgentSession(spec, self.sb, self.backend, self.verbose, self.depth + 1)
        sub.messages.append({"role": "user", "text": request})
        final_text = ""
        for _ in range(8):
            if self.sb.budget_exhausted() or self.sb.patch_submitted:
                break
            out = sub.run_turn()
            final_text = out["text"] or final_text
            if not out["had_tool_call"]:
                break
        return {"status": "ok", "agent": spec.name, "response": final_text or "(no response)"}


def build_initial_prompt(task: Dict[str, Any], sb: Sandbox, root: AgentSession) -> str:
    declared = set(root.tools)
    for s in root.sub_specs.values():
        declared.update(s.tool_names)

    lines = [
        f"You are evaluating a software engineering task for repository {task['repo']}.",
        "",
        "Problem Statement:",
        task.get("problem_statement", "").strip(),
    ]
    hints = (task.get("hints_text") or "").strip()
    if hints:
        lines += ["", "## Hints:", hints]

    lines += [
        "",
        "## Task Budget (Session terminates when any budget is exhausted)",
        f"- Time allowance: {float(sb.budget['max_time_minutes'])} minutes",
        f"- Tool calls allowance: {sb.budget['max_tool_calls']} calls",
        f"- Max loop iterations: {sb.budget['max_turns']} turns",
        "",
        "## Execution Environment Rules",
        f"- Single command timeout: {sb.budget['timeout_seconds']} seconds "
        "(commands exceeding this fail without ending the session)",
        "- Command output limit: 5000 characters",
    ]
    if "read_file" in declared:
        lines += [
            "- File view limit: 150 lines per read_file call",
            "- File character limit: 10000 characters per read_file call",
        ]
    lines += [
        "- Environment is offline (no network/PyPI access). All repository and test "
        "dependencies are ALREADY pre-installed. Do NOT attempt to run pip install.",
        "",
        "## Instructions",
        "0. Work strictly under /workspace.",
        "1. Inspect existing conventions before changing code.",
        "2. Make the minimal change that fixes the described problem.",
        "3. Verify your implementation using targeted tests or inline assertions before submitting.",
        "4. Call submit_patch once your change is complete and verified.",
        "5. Return a final text response summarising what you changed.",
    ]

    intel = {"search_similar_code", "get_code_neighbors", "get_code_subgraph"} & declared
    if intel and sb.has_code_intel():
        lines += [
            "",
            "## Code Intelligence Tools",
            "This repository has pre-built code graph and embedding data indexed by dotted "
            "symbol path (e.g. `pkg.module.Class.method`).",
        ]
        if "search_similar_code" in intel:
            lines.append("- `search_similar_code(query)`: find symbols with similar embeddings.")
        if "get_code_neighbors" in intel:
            lines.append("- `get_code_neighbors(node)`: callers and callees of a symbol.")
        if "get_code_subgraph" in intel:
            lines.append("- `get_code_subgraph(nodes)`: induced call subgraph for a list of symbols.")

    lines += ["", "## Workspace Layout", sb.workspace_layout()]
    return "\n".join(lines)


def run_autonomous(root: AgentSession, sb: Sandbox, max_turns: int) -> Dict[str, Any]:
    nudges = 0
    total_nudges = 0
    turns = 0
    end_reason = "max_turns"
    first_edit_turn: Optional[int] = None
    while turns < max_turns:
        turns += 1
        out = root.run_turn()
        if first_edit_turn is None and sb.edits_landed:
            first_edit_turn = turns
        if sb.patch_submitted:
            end_reason = "submit_patch"
            print(f"\n{C_GREEN}submit_patch called - ending session.{C_RESET}")
            break
        blocked = sb.budget_exhausted()
        if blocked:
            end_reason = blocked.replace(" ", "_")
            print(f"\n{C_YELLOW}{blocked} - ending session.{C_RESET}")
            break
        if out["had_tool_call"]:
            nudges = 0
            continue
        nudges += 1
        total_nudges += 1
        if nudges > 3:
            end_reason = "nudge_limit"
            print(f"\n{C_YELLOW}3 consecutive nudges without a tool call - ending session.{C_RESET}")
            break
        fr = str(out["finish_reason"]).upper()
        if "<|tool_call" in (out["text"] or ""):
            nudge = NUDGE_TRUNCATED
        elif "MAX_TOKENS" in fr or "LENGTH" in fr:
            nudge = NUDGE_MAXTOK
        else:
            nudge = NUDGE_NORMAL
        print(f"\n{C_YELLOW}[nudge {nudges}/3]{C_RESET} {nudge[:90]}...")
        root.messages.append({"role": "user", "text": nudge})

    patch = sb.submitted_patch
    if not patch:
        patch = sb.live_diff()
        if patch.strip():
            print(f"{C_YELLOW}No submit_patch; recovered {len(patch)} B from the working tree.{C_RESET}")
    return {
        "turns": turns,
        "tool_calls": sb.tool_calls_used,
        "elapsed_seconds": round(sb.elapsed, 1),
        "patch_submitted": sb.patch_submitted,
        "end_reason": end_reason,
        "nudges": total_nudges,
        "edits_landed": sb.edits_landed,
        "first_edit_turn": first_edit_turn,
        "tool_uses": dict(sb.tool_uses),
        "tool_errors": dict(sb.tool_errors),
        "failed_calls": sb.failed_calls,
        "patch": patch,
    }


def make_backend(kind: str, model: str, api_base: str = "", mock_script: Optional[List] = None) -> Backend:
    if kind == "mock":
        return MockBackend(mock_script or DEFAULT_MOCK_SCRIPT)
    if kind == "gemini":
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
        if not key:
            raise SystemExit("set GEMINI_API_KEY or GOOGLE_API_KEY")
        return GeminiBackend(model, api_key=key)
    return OpenAIBackend(model, api_key=os.environ.get("OPENAI_API_KEY", ""), api_base=api_base)


def run_task(
    sub: Submission,
    task: Dict[str, Any],
    snapshot: Path,
    backend: Backend,
    verify: bool = False,
    verbose: bool = True,
    keep_sandbox: bool = False,
) -> Dict[str, Any]:
    """Phase 1 (+ optional Phase 2) for one task. Shared by --auto and run_experiment.py."""
    tmp = tempfile.mkdtemp(prefix=f"swegemma_{task['instance_id']}_")
    workspace = Path(tmp) / "workspace"
    try:
        extract_snapshot_to_dir(snapshot, workspace)
        sb = Sandbox(workspace, task, dict(sub.budget))
        root = AgentSession(sub.root, sb, backend, verbose=verbose)
        root.messages.append({"role": "user", "text": build_initial_prompt(task, sb, root)})
        result = run_autonomous(root, sb, int(sub.budget["max_turns"]))
        result["instance_id"] = task["instance_id"]
        result["repo"] = task["repo"]
        result["patch_size"] = len(result["patch"])
        if verify:
            if result["patch"].strip():
                v = verify_agent_patch(task, snapshot, result["patch"])
                result["resolved"] = bool(v.get("resolved"))
                result["pytest_exit_code"] = v.get("exit_code")
                result["verify_error"] = v.get("error")
                result["test_env"] = v.get("test_env", "unknown")
            else:
                result["resolved"] = False
                result["verify_error"] = "empty patch"
        return result
    finally:
        if keep_sandbox:
            print(f"Sandbox kept at {workspace}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def classify_failure(r: Dict[str, Any]) -> str:
    """Bucket a non-resolved task so a sweep says *why* it failed, not just that it did."""
    if r.get("resolved"):
        return "resolved"
    if not r.get("patch_size"):
        if not r.get("edits_landed"):
            return "no_edit_attempted"
        return "edits_made_but_no_diff"
    err = str(r.get("verify_error") or "")
    if "apply" in err.lower():
        return "patch_did_not_apply"
    return "tests_failed"


# --------------------------------------------------------------------------------------
# REPL
# --------------------------------------------------------------------------------------

HELP = """
Commands:
  <text>            Send an instruction to the root agent and run until it stops
  /auto             Run the agent autonomously from the task prompt (as the scorer does)
  /diff             Show the current patch
  /status           Budget and patch status
  /run <cmd>        Run a shell command in the sandbox
  /cat <path>       read_file the first 150 lines of a file
  /tools            Show the compiled tool list per agent
  /tree             Show the compiled submission tree
  /prompt           Show the initial user prompt the harness built
  /verify           Run Phase 2 verification on the current patch
  /reset            Discard all changes in the sandbox
  /exit             Quit
"""


def repl(root: AgentSession, sb: Sandbox, task: Dict[str, Any], snapshot: Path,
         sub: Submission, initial_prompt: str) -> None:
    try:
        import readline  # noqa: F401
    except ImportError:
        pass
    print(f"{C_DIM}{HELP}{C_RESET}")
    while True:
        try:
            line = input(f"{C_BOLD}(harness) > {C_RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        low = line.lower()

        if low in ("/exit", "/quit"):
            break
        if low in ("/help", "help"):
            print(HELP)
            continue
        if low == "/diff":
            d = sb.live_diff()
            print(f"\n{C_BOLD}patch: {len(d)} bytes{C_RESET}")
            print(color_diff(d))
            continue
        if low == "/status":
            print(json.dumps(sb.call("get_status", {}, root.skills), indent=2))
            continue
        if low == "/tree":
            print(sub.describe())
            continue
        if low == "/prompt":
            print(initial_prompt)
            continue
        if low == "/tools":
            def show(a: AgentSession, ind: str = "") -> None:
                print(f"{ind}{a.spec.name}: {', '.join(sorted(a.tools))}")
                for s in a.sub_specs.values():
                    print(f"{ind}  {s.name}: {', '.join(s.tool_names)}")
            show(root)
            continue
        if low.startswith("/run "):
            res = sb.call("run_command", {"command": line[5:]}, root.skills)
            print(res.get("stdout") or res.get("details", {}).get("stdout", ""))
            err = res.get("stderr") or res.get("details", {}).get("stderr", "")
            if err:
                print(f"{C_RED}{err}{C_RESET}")
            print(f"exit={res.get('exit_code', res.get('details', {}).get('exit_code'))}")
            continue
        if low.startswith("/cat "):
            res = sb.call("read_file", {"filepath": line[5:].strip()}, root.skills)
            print(res.get("content") or res.get("error_message"))
            continue
        if low == "/reset":
            sb.git("checkout", "HEAD", "--", ".")
            sb.git("clean", "-fd")
            print("workspace reset to baseline")
            continue
        if low == "/verify":
            patch = sb.submitted_patch or sb.live_diff()
            res = verify_agent_patch(task, snapshot, patch)
            print(f"\nresolved={res.get('resolved')} exit_code={res.get('exit_code')}")
            continue
        if low == "/auto":
            root.messages.append({"role": "user", "text": initial_prompt})
            print(json.dumps(
                {k: v for k, v in run_autonomous(root, sb, int(sb.budget["max_turns"])).items()
                 if k != "patch"}, indent=2))
            continue

        root.messages.append({"role": "user", "text": line})
        for _ in range(int(sb.budget["max_turns"])):
            out = root.run_turn()
            if not out["had_tool_call"] or sb.patch_submitted or sb.budget_exhausted():
                break


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------

def load_task(tasks_file: Path, task_id: Optional[str], idx: int) -> Dict[str, Any]:
    tasks = [json.loads(l) for l in tasks_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    if task_id:
        for t in tasks:
            if t["instance_id"] == task_id:
                return t
        raise SystemExit(f"task {task_id} not in {tasks_file}")
    return tasks[idx]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--submission-dir", default=str(REPO_ROOT / "sample_submission"))
    ap.add_argument("--tasks", default=str(REPO_ROOT / "tasks.jsonl"))
    ap.add_argument("--snapshots-dir", default=str(REPO_ROOT / "snapshots"))
    ap.add_argument("--task-id")
    ap.add_argument("--task-idx", type=int, default=0)
    ap.add_argument("--backend", choices=["gemini", "openai", "mock"], default="gemini")
    ap.add_argument("--mock-script", help="JSON list of scripted turns for --backend mock")
    ap.add_argument(
        "--verify-patch",
        help="Skip the agent: run Phase 2 verification on this .patch file and exit",
    )
    ap.add_argument("--model", help="override the model the submission declares")
    ap.add_argument("--api-base", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--auto", action="store_true", help="run autonomously instead of the REPL")
    ap.add_argument("--verify", action="store_true", help="run Phase 2 after an --auto run")
    ap.add_argument("--dry-run", action="store_true", help="validate and print, no API calls")
    ap.add_argument("--keep-sandbox", action="store_true")
    ap.add_argument("--max-tool-calls", type=int)
    ap.add_argument("--max-time-minutes", type=float)
    ap.add_argument("--results-dir", default=str(REPO_ROOT / "results" / "manual"))
    ap.add_argument(
        "--test-env",
        choices=["auto", "shared"],
        default="auto",
        help="auto resolves each snapshot's own pins into a cached venv for Phase 2; "
             "shared uses this repo's .venv for every task (the old behaviour)",
    )
    args = ap.parse_args()

    # verify_agent_patch reads this; passing it through the environment keeps the verifier free of
    # a dependency on this script's argument parser.
    os.environ["SWE_TEST_ENV"] = args.test_env

    sub = Submission(Path(args.submission_dir))
    print("=" * 78)
    print(f"{C_BOLD}  COMPILED SUBMISSION{C_RESET}  {sub.dir}")
    print("=" * 78)
    print(sub.describe())

    problems = sub.validate()
    print()
    if problems:
        print(f"{C_RED}{C_BOLD}VALIDATION PROBLEMS ({len(problems)}){C_RESET}")
        for p in problems:
            print(f"  {C_RED}x{C_RESET} {p}")
    else:
        print(f"{C_GREEN}Validation clean: single allowed model, tools known, skills load.{C_RESET}")

    if args.max_tool_calls:
        sub.budget["max_tool_calls"] = args.max_tool_calls
    if args.max_time_minutes:
        sub.budget["max_time_minutes"] = args.max_time_minutes

    task = load_task(Path(args.tasks), args.task_id, args.task_idx)
    snapshot = Path(args.snapshots_dir) / f"{task['instance_id']}.tgz"
    if not snapshot.exists():
        raise SystemExit(f"snapshot not found: {snapshot}")

    if args.verify_patch:
        patch_text = Path(args.verify_patch).read_text(encoding="utf-8")
        v = verify_agent_patch(task, snapshot, patch_text)
        ok = bool(v.get("resolved"))
        print(
            f"\n{'='*60}\n  PHASE 2: "
            f"{(C_GREEN + 'RESOLVED (1.0)') if ok else (C_RED + 'FAILED (0.0)')}{C_RESET}"
            f"\n  pytest exit code: {v.get('exit_code')}\n{'='*60}"
        )
        raise SystemExit(0 if ok else 1)

    declared_model = sub.root.model or ""
    if args.model:
        model = args.model
    elif args.backend == "mock":
        model = "mock"
    elif args.backend == "gemini":
        model = "gemini-2.5-flash"
        print(
            f"{C_YELLOW}Note: the submission declares {declared_model!r}; testing with "
            f"{model!r}. Gemini's function calling is more forgiving than vLLM's gemma4 "
            f"parser, so a pass here is not proof.{C_RESET}"
        )
    else:
        model = declared_model

    backend = make_backend(
        args.backend,
        model,
        api_base=args.api_base,
        mock_script=(
            json.loads(Path(args.mock_script).read_text(encoding="utf-8"))
            if args.mock_script
            else None
        ),
    ) if not (args.dry_run and args.backend == "gemini") else MockBackend([])

    if args.auto:
        print(f"\n{C_BOLD}Task:{C_RESET} {task['instance_id']}  {C_BOLD}Model:{C_RESET} {model}")
        result = run_task(
            sub, task, snapshot, backend,
            verify=args.verify, keep_sandbox=args.keep_sandbox,
        )
        patch = result.pop("patch")
        out_dir = Path(args.results_dir) / task["instance_id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{task['instance_id']}.patch").write_text(patch, encoding="utf-8")
        print(f"\n{C_BOLD}--- PATCH ({len(patch)} bytes) ---{C_RESET}")
        print(color_diff(patch))
        if args.verify:
            ok = result.get("resolved")
            print(
                f"\n{'='*60}\n  PHASE 2: "
                f"{(C_GREEN + 'RESOLVED (1.0)') if ok else (C_RED + 'FAILED (0.0)')}"
                f"{C_RESET}\n{'='*60}"
            )
        result["failure_bucket"] = classify_failure(result)
        (out_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        print(f"\nArtifacts: {out_dir}")
        return

    # dry-run and REPL both need a live sandbox
    tmp = tempfile.mkdtemp(prefix=f"swegemma_manual_{task['instance_id']}_")
    workspace = Path(tmp) / "workspace"
    print(f"\n{C_CYAN}Unpacking snapshot into {workspace}{C_RESET}")
    extract_snapshot_to_dir(snapshot, workspace)
    sb = Sandbox(workspace, task, sub.budget)
    root = AgentSession(sub.root, sb, backend)
    initial_prompt = build_initial_prompt(task, sb, root)

    try:
        if args.dry_run:
            print(f"\n{C_BOLD}--- tools exposed to {sub.root.name} ---{C_RESET}")
            for n in sorted(root.tools):
                print(f"  {n}")
            print(
                f"\n{C_BOLD}--- system instruction "
                f"({len(root.system_instruction())} chars) ---{C_RESET}"
            )
            print(root.system_instruction()[:1500] + "\n...")
            print(f"\n{C_BOLD}--- initial user prompt ({len(initial_prompt)} chars) ---{C_RESET}")
            print(initial_prompt[:2500] + "\n...")
            print(f"\n{C_GREEN}Dry run complete: no API calls were made.{C_RESET}")
            return
        repl(root, sb, task, snapshot, sub, initial_prompt)
    finally:
        if args.keep_sandbox:
            print(f"Sandbox kept at {workspace}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
