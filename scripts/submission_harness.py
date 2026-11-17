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
import re
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
    _pythonpath_for,
    extract_snapshot_to_dir,
    verify_agent_patch,
)
from scripts.test_envs import resolve_pytest  # noqa: E402

# Where real Gemma 4 is served. Override with --api-base or SWE_API_BASE.
DEFAULT_API_BASE = "http://10.120.12.55:8000/v1"

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


AGENT_CLASSES = ("LlmAgent", "SequentialAgent", "LoopAgent", "ParallelAgent")
WORKFLOW_CLASSES = ("SequentialAgent", "LoopAgent", "ParallelAgent")

# ADK substitutes {name} / {name?} in an instruction from session.state. A name that is not a valid
# identifier is left alone; a valid one that is missing raises KeyError unless it ends in "?".
STATE_KEY_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z_][A-Za-z0-9_]*)?)(\?)?\}")
HARNESS_STATE_KEYS = {"problem_description", "hints"}


class StateKeyError(Exception):
    """An instruction references a session-state key that is not set (ADK raises KeyError)."""


def render_instruction(template: str, state: Dict[str, Any]) -> str:
    def sub(m: "re.Match[str]") -> str:
        key, optional = m.group(1), m.group(2)
        if key in state:
            return str(state[key])
        if optional:
            return ""
        raise StateKeyError(f"instruction references missing session-state key {{{key}}}")

    return STATE_KEY_RE.sub(sub, template)


class AgentSpec:
    def __init__(self, cfg: Dict[str, Any], root: Path, source: Path):
        self.source = source
        self.name: str = cfg.get("name") or source.stem
        self.agent_class: str = cfg.get("agent_class") or "LlmAgent"
        if self.agent_class not in AGENT_CLASSES:
            raise SubmissionError(f"{source}: unsupported agent_class {self.agent_class!r}")
        self.description: str = cfg.get("description", "")
        self.model: Optional[str] = cfg.get("model")
        self.instruction: str = cfg.get("instruction", "") or ""
        self.gen_cfg: Dict[str, Any] = cfg.get("generate_content_config") or {}
        self.output_key: Optional[str] = cfg.get("output_key")
        self.include_contents: str = cfg.get("include_contents") or "default"
        if self.include_contents not in ("default", "none"):
            raise SubmissionError(f"{source}: include_contents must be 'default' or 'none'")
        self.max_iterations: int = int(cfg.get("max_iterations") or 500)
        if not 1 <= self.max_iterations <= 500:
            raise SubmissionError(f"{source}: max_iterations must be within 1..500")
        self.tool_names: List[str] = []
        self.agent_tools: List["AgentSpec"] = []
        self.sub_agents: List["AgentSpec"] = []
        self.skills: List[Dict[str, Any]] = []

        for entry in cfg.get("sub_agents") or []:
            cp = entry.get("config_path") if isinstance(entry, dict) else None
            if not cp:
                raise SubmissionError(f"{source}: sub_agents entry needs config_path: {entry!r}")
            if cp.startswith("/") or ".." in Path(cp).parts:
                raise SubmissionError(f"{source}: sub_agents path rejected: {cp}")
            sub_path = (source.parent / cp).resolve()
            if not str(sub_path).startswith(str(root.resolve())) or not sub_path.exists():
                raise SubmissionError(f"{source}: sub_agent not found: {cp}")
            self.sub_agents.append(
                AgentSpec(load_yaml_with_includes(sub_path, root), root, sub_path)
            )

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

    @property
    def is_workflow(self) -> bool:
        return self.agent_class in WORKFLOW_CLASSES

    def all_agents(self) -> List["AgentSpec"]:
        out = [self]
        for a in self.sub_agents + self.agent_tools:
            out.extend(a.all_agents())
        return out

    def llm_agents(self) -> List["AgentSpec"]:
        return [a for a in self.all_agents() if not a.is_workflow]

    def state_keys_used(self) -> List[Tuple[str, bool]]:
        """(key, optional) pairs referenced by this agent's instruction."""
        return [(m.group(1), bool(m.group(2))) for m in STATE_KEY_RE.finditer(self.instruction)]


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
            if not a.model and not a.is_workflow:
                problems.append(f"agent {a.name} ({a.source.name}) declares no model")
            if a.is_workflow and not a.sub_agents:
                problems.append(f"{a.agent_class} {a.name} has no sub_agents")
        # The scorer's discovery also reads standalone agent YAMLs that nothing references, so a
        # leftover sub-agent file declaring another model fails the single-model rule.
        for y in sorted(list(self.dir.rglob("*.yaml")) + list(self.dir.rglob("*.yml"))):
            try:
                txt = y.read_text(encoding="utf-8")
            except OSError:
                continue
            for m in re.finditer(r"(?m)^model:\s*['\"]?([^\s'\"#]+)", txt):
                if m.group(1) not in models:
                    models.add(m.group(1))
                    problems.append(
                        f"unreferenced YAML {y.relative_to(self.dir)} declares model {m.group(1)!r}"
                    )
        if len(models) > 1:
            problems.append(f"multiple base models declared: {sorted(models)}")
        for m in models:
            if m.split("/")[-1] not in ALLOWED_MODEL_NAMES:
                problems.append(f"model {m!r} is not in ALLOWED_MODEL_NAMES")

        names = [a.name for a in agents]
        for n in sorted({n for n in names if names.count(n) > 1}):
            problems.append(f"duplicate agent name {n!r}")

        # Session-state templating. Keys set by the harness, plus every output_key in the tree.
        # A required key that may be unset at run time makes ADK raise KeyError mid-task.
        produced = {a.output_key for a in agents if a.output_key}
        for a in agents:
            for key, optional in a.state_keys_used():
                if key == "problem_description":
                    continue
                if key == "hints" and not optional:
                    problems.append(
                        f"agent {a.name}: {{hints}} is unset when a task has no hints; use {{hints?}}"
                    )
                elif key in produced and not optional:
                    problems.append(
                        f"agent {a.name}: {{{key}}} is only set if the producing agent ends with "
                        f"a text response; use {{{key}?}}"
                    )
                elif key not in produced and key not in HARNESS_STATE_KEYS:
                    problems.append(
                        f"agent {a.name}: {{{key}}} is not a session-state key "
                        "(ADK templates braces; remove them or add an output_key)"
                    )

        for a in agents:
            if a.is_workflow:
                continue
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
            if a.is_workflow:
                extra = f" max_iterations={a.max_iterations}" if a.agent_class == "LoopAgent" else ""
                lines.append(f"{indent}{a.name}  <{a.agent_class}>{extra}")
                for sub in a.sub_agents:
                    walk(sub, indent + "    ")
                return
            flags = []
            if a.include_contents != "default":
                flags.append(f"include_contents={a.include_contents}")
            if a.output_key:
                flags.append(f"output_key={a.output_key}")
            lines.append(
                f"{indent}{a.name}  [{a.model}]  instruction={len(a.instruction)} chars"
                + (f"  {' '.join(flags)}" if flags else "")
            )
            for sub in a.sub_agents:
                walk(sub, indent + "    ")
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

MAX_MODEL_LEN = 32768          # vLLM max_model_len on the scorer
COMPACTION_TOKEN_THRESHOLD = 14336   # EventsCompactionConfig.token_threshold
COMPACTION_RETENTION = 5       # EventsCompactionConfig.event_retention_size
COMPACTION_SUMMARY_TOKENS = 400

# Assumed serving speeds on 4x L4 (tokens/second). These are placeholders until measured with
# --probe-endpoint against a real vLLM server; they are deliberately on the slow side.
SIM_PRESETS: Dict[str, Dict[str, float]] = {
    "gemma12b": {"prefill_tps": 1000.0, "decode_tps": 20.0},
    "gemma31b": {"prefill_tps": 500.0, "decode_tps": 9.0},
    "gemma-e4b": {"prefill_tps": 2500.0, "decode_tps": 45.0},
}


def estimate_tokens(obj: Any) -> int:
    """Conservative token count for text or a JSON-able payload (3.0 characters per token).

    Measured against the Gemma 4 tokenizer on vLLM: Python source 3.40, Markdown prompts 3.59,
    JSON tool results 3.09 characters per token. 3.0 errs on the side of over-counting, so the
    32,768-token overflow check and the compaction trigger fire no later than on the scorer.
    """
    if not isinstance(obj, str):
        obj = json.dumps(obj, ensure_ascii=False, default=str)
    return int(len(obj) / 3.0) + 1


def make_sim(args: Any) -> Optional[Dict[str, float]]:
    """Build the simulated-clock config from --sim / --sim-prefill-tps / --sim-decode-tps."""
    sim: Dict[str, float] = {}
    preset = getattr(args, "sim", None) or os.environ.get("SWE_SIM")
    if preset:
        if preset not in SIM_PRESETS:
            raise SystemExit(f"unknown --sim preset {preset!r}; choose from {sorted(SIM_PRESETS)}")
        sim.update(SIM_PRESETS[preset])
    if getattr(args, "sim_prefill_tps", None):
        sim["prefill_tps"] = float(args.sim_prefill_tps)
    if getattr(args, "sim_decode_tps", None):
        sim["decode_tps"] = float(args.sim_decode_tps)
    if not sim:
        return None
    sim.setdefault("prefill_tps", 1000.0)
    sim.setdefault("decode_tps", 20.0)
    return sim


class Sandbox:
    def __init__(self, workspace: Path, task: Dict[str, Any], budget: Dict[str, Any],
                 sim: Optional[Dict[str, float]] = None, agent_env: str = "auto"):
        self.ws = workspace.resolve()
        self.task = task
        self.budget = budget
        # Session state as the real harness seeds it: `hints` only exists when non-empty.
        self.state: Dict[str, Any] = {"problem_description": task.get("problem_statement", "")}
        if (task.get("hints_text") or "").strip():
            self.state["hints"] = task["hints_text"].strip()
        # Simulated Gemma clock. With `sim` set, elapsed time is projected from token counts so
        # that max_time_minutes binds the way it will on 4x L4 instead of at API speed.
        self.sim = sim
        self.sim_clock = 0.0
        self.llm_calls = 0
        self.prompt_tokens_total = 0
        self.output_tokens_total = 0
        self.prompt_tokens_max = 0
        self.compactions = 0
        self.context_overflow = False
        self.phase_stats: Dict[str, Dict[str, int]] = {}
        self.trace: List[Dict[str, Any]] = []
        # Per-task scratch dir standing in for the container's /tmp, so scratch files and notes
        # from one task are never visible to the next.
        self.tmp = self.ws.parent / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.env = self._build_env(agent_env)
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
        self.max_turns = int(budget.get("max_turns") or 500)
        self.first_edit_call: Optional[int] = None
        self.graph: Optional[CodeGraph] = None
        self.emb: Optional[CodeEmbeddingIndex] = None
        self._load_code_intel()

    # -- budget -------------------------------------------------------------------
    def _build_env(self, mode: str) -> Dict[str, str]:
        """Environment for run_command, matching Container A as closely as a host can.

        The real sandbox has the repo importable and its test dependencies installed. Running
        commands with the harness's own interpreter instead made `python3 /tmp/repro.py` and
        `python -m pytest` fail for reasons the agent could not fix, and leaked host paths.
        """
        env = dict(os.environ)
        env["TMPDIR"] = str(self.tmp)
        env["TEST_TMPDIR"] = str(self.tmp)
        env["PYTHONPATH"] = _pythonpath_for(self.ws)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("VIRTUAL_ENV", None)
        self.agent_env_key = "host"
        if mode == "host":
            return env
        try:
            info = resolve_pytest(self.ws, self.task.get("repo", ""), mode=mode, verbose=False)
        except Exception as e:  # noqa: BLE001 - a broken env must not lose the task
            print(f"{C_YELLOW}[agent-env] could not resolve test env: {e}{C_RESET}")
            return env
        self.agent_env_key = info.get("env_key", "unknown")
        bin_dir = Path(info["pytest"]).parent if os.sep in str(info.get("pytest", "")) else None
        if bin_dir and bin_dir.is_dir():
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            if not (bin_dir / "python").exists() and (bin_dir / "python3").exists():
                try:
                    (bin_dir / "python").symlink_to(bin_dir / "python3")
                except OSError:
                    pass
        return env

    def _rewrite_paths(self, cmd: str) -> str:
        cmd = cmd.replace("/workspace", str(self.ws))
        return re.sub(r"(?<![\w./-])/tmp(?=/|\b)", str(self.tmp), cmd)

    def advance_clock(self, seconds: float) -> None:
        self.sim_clock += max(0.0, seconds)

    @property
    def elapsed(self) -> float:
        if self.sim:
            return self.sim_clock
        return time.time() - self.start_time

    @property
    def time_remaining(self) -> float:
        return float(self.budget["max_time_minutes"]) * 60.0 - self.elapsed

    @property
    def calls_remaining(self) -> int:
        return int(self.budget["max_tool_calls"]) - self.tool_calls_used

    def budget_exhausted(self, include_turns: bool = True) -> Optional[str]:
        if self.time_remaining <= 0:
            return "time budget exhausted"
        if self.calls_remaining <= 0:
            return "tool-call budget exhausted"
        if include_turns and self.llm_calls >= self.max_turns:
            return "max turns"
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
            blocked = self.budget_exhausted(include_turns=False)
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
            if self.first_edit_call is None:
                self.first_edit_call = self.llm_calls
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
                cmd = self._rewrite_paths(str(args.get("command", "")))
                timeout = min(
                    int(self.budget["timeout_seconds"]), max(5, int(self.time_remaining))
                )
                t0 = time.perf_counter()
                try:
                    proc = subprocess.run(
                        ["/bin/bash", "-c", cmd],
                        cwd=str(self.ws),
                        capture_output=True,
                        text=True,
                        errors="replace",
                        timeout=timeout,
                        env=self.env,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except subprocess.TimeoutExpired:
                    self.advance_clock(timeout)
                    return _err("TimeoutExceeded", f"Command timed out after {timeout}s")
                self.advance_clock(time.perf_counter() - t0)
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
        um = data.get("usageMetadata") or {}
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
            "usage": {
                "prompt_tokens": um.get("promptTokenCount"),
                "output_tokens": um.get("candidatesTokenCount"),
                "thinking_tokens": um.get("thoughtsTokenCount") or 0,
            },
        }


class OpenAIBackend(Backend):
    """Any OpenAI-compatible /v1 endpoint: vLLM serving real Gemma 4, or LiteLLM."""

    # Set SWE_OPENAI_PLAIN=1 for endpoints that reject vLLM-only fields (top_k, thinking).
    plain_openai = bool(os.environ.get("SWE_OPENAI_PLAIN"))

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
        if "top_k" in gen_cfg and not self.plain_openai:
            payload["top_k"] = gen_cfg["top_k"]
        if "seed" in gen_cfg:
            payload["seed"] = gen_cfg["seed"]
        # LiteLLM's `extra_body` is merged into the top level of the request, so over raw HTTP
        # these fields belong at the top level. thinking_budget 0 must switch thinking off
        # explicitly, otherwise the chat template's default applies.
        tcfg = gen_cfg.get("thinking_config") or {}
        if not self.plain_openai:
            budget = tcfg.get("thinking_budget")
            if budget is not None and int(budget) > 0:
                payload["chat_template_kwargs"] = {"enable_thinking": True}
                payload["thinking_token_budget"] = int(budget)
            elif budget is not None or tcfg.get("include_thoughts") is False:
                payload["chat_template_kwargs"] = {"enable_thinking": False}
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
        usage = data.get("usage") or {}
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        # vLLM's completion_tokens already includes reasoning tokens; split them out so the
        # simulated clock does not count thinking twice.
        details = usage.get("completion_tokens_details") or {}
        thinking = details.get("reasoning_tokens")
        if thinking is None:
            thinking = estimate_tokens(reasoning) if reasoning else 0
        completion = usage.get("completion_tokens")
        visible = max(int(completion) - int(thinking), 0) if completion is not None else None
        return {
            "text": msg.get("content") or "",
            "tool_calls": calls,
            "finish_reason": choice.get("finish_reason", ""),
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens"),
                "output_tokens": visible,
                "thinking_tokens": int(thinking),
            },
        }

    def probe(self) -> Dict[str, Any]:
        """Check the endpoint: list models, one tool-call round trip, measured speed."""
        out: Dict[str, Any] = {"api_base": self.api_base, "model": self.model}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        req = urllib.request.Request(f"{self.api_base}/models", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            listed = json.loads(resp.read().decode("utf-8"))
        out["models"] = [m.get("id") for m in listed.get("data") or []]
        out["max_model_len"] = next(
            (m.get("max_model_len") for m in listed.get("data") or [] if m.get("max_model_len")),
            None,
        )
        tools = {"run_command": TOOL_SCHEMAS["run_command"]}
        cfg = {"max_output_tokens": 256, "temperature": 0.0, "thinking_config": {"thinking_budget": 0}}
        t0 = time.perf_counter()
        r = self.generate(
            "You are a coding agent. Always act by calling a tool.",
            [{"role": "user", "text": "List the files in the current directory."}],
            tools, cfg,
        )
        out["tool_call_seconds"] = round(time.perf_counter() - t0, 2)
        out["tool_call_parsed"] = bool(r["tool_calls"]) and r["tool_calls"][0]["name"] == "run_command"
        out["tool_call_raw_text"] = r["text"][:200]
        # Decode speed: a long free-text completion. Prefill speed: the same with a long prompt.
        cfg = {"max_output_tokens": 400, "temperature": 0.0, "thinking_config": {"thinking_budget": 0}}
        t0 = time.perf_counter()
        r = self.generate("", [{"role": "user", "text": "Count from 1 to 300, one number per line."}], {}, cfg)
        dt = time.perf_counter() - t0
        n_out = r["usage"].get("output_tokens") or estimate_tokens(r["text"])
        out["decode_tps"] = round(n_out / dt, 1) if dt > 0 else None
        filler = "def f(x):\n    return x + 1\n" * 1500
        cfg = {"max_output_tokens": 4, "temperature": 0.0, "thinking_config": {"thinking_budget": 0}}
        t0 = time.perf_counter()
        r = self.generate("", [{"role": "user", "text": filler + "\nReply with OK."}], {}, cfg)
        dt = time.perf_counter() - t0
        n_in = r["usage"].get("prompt_tokens") or estimate_tokens(filler)
        out["prefill_tps"] = round(n_in / dt, 1) if dt > 0 else None
        return out


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
    """One LlmAgent bound to a conversation.

    `events` is the session event list. Agents of a SequentialAgent/LoopAgent share one list, as
    they share one ADK session; an AgentTool child gets its own. Each event carries its author so
    that include_contents and the "For context: [agent] said" rewriting can be reproduced.
    """

    def __init__(self, spec: AgentSpec, sandbox: Sandbox, backend: Backend,
                 verbose: bool = True, depth: int = 0,
                 events: Optional[List[Dict[str, Any]]] = None):
        self.spec = spec
        self.sb = sandbox
        self.backend = backend
        self.verbose = verbose
        self.depth = depth
        self.events: List[Dict[str, Any]] = events if events is not None else []
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
                                "conversation, so include file paths, line numbers and exactly "
                                "what you want returned."
                            ),
                        }
                    },
                    "required": ["request"],
                },
            }
        self.sub_specs = {s.name: s for s in spec.agent_tools}
        self._cached_tokens = 0  # prompt prefix the server already holds for this agent

    # Kept for the REPL and older callers, which append {"role": "user", "text": ...} directly.
    @property
    def messages(self) -> List[Dict[str, Any]]:
        return self.events

    def system_instruction(self) -> str:
        parts = [render_instruction(self.spec.instruction, self.sb.state)]
        if self.skills:
            lines = ["", "## Available Skills", "Call load_skill(skill_name) to read one in full."]
            for s in self.skills.values():
                lines.append(f"- `{s['name']}`: {s['description']}")
            parts.append("\n".join(lines))
        return "\n".join(parts)

    def overhead_tokens(self) -> int:
        """Tokens sent on every request before any conversation: instruction + tool schemas."""
        try:
            system = self.system_instruction()
        except StateKeyError:
            system = self.spec.instruction
        return estimate_tokens(system) + estimate_tokens(
            [{"name": n, **s} for n, s in self.tools.items()]
        )

    def build_messages(self) -> List[Dict[str, Any]]:
        """The contents this agent is sent, following ADK's include_contents rules."""
        me = self.spec.name
        evs = self.events
        if self.spec.include_contents == "none":
            # Current turn only: from the latest event written by the user or another agent.
            start = 0
            for i in range(len(evs) - 1, -1, -1):
                if evs[i].get("author", "user") != me:
                    start = i
                    break
            evs = evs[start:]
        out: List[Dict[str, Any]] = []
        for e in evs:
            author = e.get("author", "user")
            if author in ("user", me):
                m = {k: v for k, v in e.items() if k != "author"}
            else:
                # Another agent's events reach this one as plain user-role context.
                if e["role"] == "tool":
                    text = (f"For context: [{author}] `{e.get('name')}` tool returned result: "
                            f"{json.dumps(e.get('content'), default=str)}")
                else:
                    bits = []
                    if (e.get("text") or "").strip():
                        bits.append(f"For context: [{author}] said: {e['text']}")
                    for tc in e.get("tool_calls") or []:
                        bits.append(f"For context: [{author}] called tool `{tc['name']}` with "
                                    f"parameters: {json.dumps(tc['args'], default=str)}")
                    text = "\n".join(bits)
                if not text:
                    continue
                m = {"role": "user", "text": text}
            if (out and m["role"] == "user" and out[-1]["role"] == "user"
                    and not out[-1].get("tool_calls")):
                # Chat templates for Gemma reject two consecutive user turns; merge them.
                out[-1] = {"role": "user", "text": out[-1].get("text", "") + "\n\n" + m.get("text", "")}
            else:
                out.append(m)
        return out

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(("    " * self.depth) + msg)

    def _compact(self) -> None:
        """Stand-in for ADK events compaction: older events collapse into one summary event.

        The real summary is written by an extra LLM call; here it is a mechanical digest, and the
        cost of that call is charged to the simulated clock. What matters for testing is that the
        agent loses its early context, exactly as it will on the scorer.
        """
        evs = self.events
        cut = len(evs) - COMPACTION_RETENTION
        while cut > 0 and evs[cut]["role"] == "tool":
            cut -= 1
        if cut <= 1:
            return
        dropped = evs[:cut]
        first_user = next((e.get("text", "") for e in dropped if e["role"] == "user"), "")
        actions = []
        for e in dropped:
            for tc in e.get("tool_calls") or []:
                actions.append(f"- {e.get('author')}: {tc['name']}({json.dumps(tc['args'], default=str)[:100]})")
        summary = (
            "[Earlier conversation was compacted. Summary follows.]\n"
            + first_user[:2000]
            + "\n\nActions taken so far:\n"
            + "\n".join(actions[-30:])
        )
        self.events[:] = [{"author": "user", "role": "user", "text": summary}] + evs[cut:]
        self.sb.compactions += 1
        self._cached_tokens = 0
        if self.sb.sim:
            self.sb.advance_clock(
                estimate_tokens(dropped) / self.sb.sim["prefill_tps"]
                + COMPACTION_SUMMARY_TOKENS / self.sb.sim["decode_tps"]
            )
        self.sb.trace.append({"t": round(self.sb.elapsed, 1), "agent": self.spec.name,
                              "kind": "compaction", "dropped_events": len(dropped)})
        self._log(f"{C_YELLOW}[context compacted: {len(dropped)} events summarised]{C_RESET}")

    def run_turn(self) -> Dict[str, Any]:
        """One model request plus the tool calls it asks for."""
        sb = self.sb
        system = self.system_instruction()
        messages = self.build_messages()
        if (estimate_tokens(messages) > COMPACTION_TOKEN_THRESHOLD
                and len(self.events) > COMPACTION_RETENTION + 1):
            self._compact()
            messages = self.build_messages()

        tool_decl = [{"name": n, **s} for n, s in self.tools.items()]
        est_prompt = estimate_tokens(system) + estimate_tokens(messages) + estimate_tokens(tool_decl)
        max_out = int(self.spec.gen_cfg.get("max_output_tokens", 16384))
        if est_prompt + max_out > MAX_MODEL_LEN:
            # vLLM rejects a request whose prompt plus max_tokens exceeds max_model_len.
            sb.context_overflow = True
            sb.trace.append({"t": round(sb.elapsed, 1), "agent": self.spec.name,
                             "kind": "context_overflow", "prompt_tokens": est_prompt,
                             "max_output_tokens": max_out})
            self._log(f"{C_RED}[context overflow: ~{est_prompt} prompt + {max_out} output "
                      f"> {MAX_MODEL_LEN}]{C_RESET}")
            return {"had_tool_call": False, "text": "", "finish_reason": "CONTEXT_OVERFLOW"}

        t0 = time.perf_counter()
        resp = self.backend.generate(system, messages, self.tools, self.spec.gen_cfg)
        latency = time.perf_counter() - t0

        usage = resp.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or est_prompt)
        visible_out = int(usage.get("output_tokens") or estimate_tokens(
            (resp.get("text") or "") + json.dumps(resp.get("tool_calls") or [], default=str)))
        thinking = int(usage.get("thinking_tokens") or 0)
        budget = (self.spec.gen_cfg.get("thinking_config") or {}).get("thinking_budget")
        if budget is not None:
            thinking = min(thinking, int(budget))
        out_tokens = min(visible_out + thinking, max_out)

        if sb.sim:
            fresh = max(prompt_tokens - self._cached_tokens, 0)
            sb.advance_clock(fresh / sb.sim["prefill_tps"] + out_tokens / sb.sim["decode_tps"])
        self._cached_tokens = prompt_tokens + visible_out

        sb.llm_calls += 1
        sb.prompt_tokens_total += prompt_tokens
        sb.output_tokens_total += out_tokens
        sb.prompt_tokens_max = max(sb.prompt_tokens_max, prompt_tokens)
        ps = sb.phase_stats.setdefault(
            self.spec.name, {"llm_calls": 0, "tool_calls": 0, "prompt_tokens": 0, "output_tokens": 0})
        ps["llm_calls"] += 1
        ps["prompt_tokens"] += prompt_tokens
        ps["output_tokens"] += out_tokens
        sb.trace.append({
            "t": round(sb.elapsed, 1), "agent": self.spec.name, "kind": "llm",
            "prompt_tokens": prompt_tokens, "output_tokens": out_tokens,
            "thinking_tokens": thinking, "latency": round(latency, 2),
            "finish_reason": resp.get("finish_reason", ""),
            "text": (resp.get("text") or "")[:400],
            "tool_calls": [tc["name"] for tc in resp["tool_calls"]],
        })

        self.events.append({"author": self.spec.name, "role": "assistant",
                            "text": resp["text"], "tool_calls": resp["tool_calls"]})
        if resp["text"].strip():
            self._log(f"\n{C_BOLD}{self.spec.name} ({latency:.1f}s):{C_RESET}")
            self._log(resp["text"].strip()[:4000])

        for tc in resp["tool_calls"]:
            name, args = tc["name"], tc["args"]
            preview = json.dumps(args, default=str)[:110]
            self._log(f"{C_YELLOW}-> {C_BOLD}{name}{C_RESET}({preview})")

            if name in self.sub_specs:
                result = self._run_sub_agent(self.sub_specs[name], str(args.get("request", "")))
            elif name not in self.tools:
                # ToolErrorPlugin: an undeclared tool comes back as a structured error.
                result = _err("UnknownTool", f"Tool {name!r} is not available to {self.spec.name}")
                sb.tool_errors[f"{name}:UnknownTool"] += 1
            else:
                result = sb.call(name, args, self.skills)
            ps["tool_calls"] += 1
            sb.trace.append({
                "t": round(sb.elapsed, 1), "agent": self.spec.name, "kind": "tool", "name": name,
                "arg": str(args.get("command") or args.get("filepath") or args.get("query")
                           or args.get("node") or args.get("request") or "")[:300],
                "status": result.get("status"), "error_type": result.get("error_type"),
                "result_chars": len(json.dumps(result, default=str)),
            })

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

            self.events.append({"author": self.spec.name, "role": "tool", "name": name,
                                "tool_call_id": tc["id"], "content": result})

        return {
            "had_tool_call": bool(resp["tool_calls"]),
            "text": resp["text"],
            "finish_reason": resp.get("finish_reason", ""),
        }

    def run(self) -> Dict[str, Any]:
        """Run this agent until it answers without a tool call, as one ADK agent run does."""
        any_tool = False
        out: Dict[str, Any] = {"had_tool_call": False, "text": "", "finish_reason": ""}
        while True:
            if self.sb.patch_submitted or self.sb.budget_exhausted() or self.sb.context_overflow:
                break
            out = self.run_turn()
            any_tool = any_tool or out["had_tool_call"]
            if not out["had_tool_call"]:
                if self.spec.output_key and (out["text"] or "").strip():
                    self.sb.state[self.spec.output_key] = out["text"].strip()
                break
        return {**out, "had_tool_call": any_tool}

    def _run_sub_agent(self, spec: AgentSpec, request: str) -> Dict[str, Any]:
        """AgentTool: the child runs in its own conversation and returns its final text."""
        self._log(f"   {C_CYAN}[delegating to {spec.name}]{C_RESET}")
        try:
            out = run_agent(spec, self.sb, self.backend, self.verbose,
                            [{"author": "user", "role": "user", "text": request}], self.depth + 1)
        except StateKeyError as e:
            return _err("KeyError", str(e))
        return {"status": "ok", "agent": spec.name, "response": out.get("text") or "(no response)"}


def run_agent(spec: AgentSpec, sb: Sandbox, backend: Backend, verbose: bool,
              events: List[Dict[str, Any]], depth: int = 0) -> Dict[str, Any]:
    """Execute one agent of any class against a shared event list."""
    if not spec.is_workflow:
        return AgentSession(spec, sb, backend, verbose, depth, events).run()

    def stopped() -> bool:
        return bool(sb.patch_submitted or sb.budget_exhausted() or sb.context_overflow)

    any_tool = False
    out: Dict[str, Any] = {"had_tool_call": False, "text": "", "finish_reason": ""}
    rounds = spec.max_iterations if spec.agent_class == "LoopAgent" else 1
    for _ in range(rounds):
        for child in spec.sub_agents:  # ParallelAgent branches are run one after another here
            if stopped():
                return {**out, "had_tool_call": any_tool}
            out = run_agent(child, sb, backend, verbose, events, depth)
            any_tool = any_tool or out["had_tool_call"]
        if stopped():
            break
    return {**out, "had_tool_call": any_tool}


class RootRunner:
    """The compiled submission plus its session: what the scorer drives with run_async."""

    def __init__(self, spec: AgentSpec, sandbox: Sandbox, backend: Backend, verbose: bool = True):
        self.spec = spec
        self.sb = sandbox
        self.backend = backend
        self.verbose = verbose
        self.events: List[Dict[str, Any]] = []

    def add_user(self, text: str) -> None:
        self.events.append({"author": "user", "role": "user", "text": text})

    def invoke(self) -> Dict[str, Any]:
        return run_agent(self.spec, self.sb, self.backend, self.verbose, self.events)

    def sessions(self) -> List[AgentSession]:
        return [AgentSession(a, self.sb, self.backend, self.verbose) for a in self.spec.llm_agents()]


def declared_tools(spec: AgentSpec) -> set:
    out: set = set()
    for a in spec.llm_agents():
        out.update(a.tool_names)
    return out


def build_initial_prompt(task: Dict[str, Any], sb: Sandbox, root: AgentSpec) -> str:
    declared = declared_tools(root)

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


def run_autonomous(root: "RootRunner", sb: Sandbox, max_turns: Optional[int] = None) -> Dict[str, Any]:
    """The scorer's outer loop: invoke the root agent, nudge, repeat until a stop condition."""
    if max_turns is not None:
        sb.max_turns = int(max_turns)
    nudges = 0
    total_nudges = 0
    invocations = 0
    end_reason = "nudge_limit"
    wall0 = time.time()
    while True:
        invocations += 1
        try:
            out = root.invoke()
        except StateKeyError as e:
            end_reason = "state_key_error"
            print(f"\n{C_RED}{e} - the scorer would abort this task.{C_RESET}")
            break
        if sb.patch_submitted:
            end_reason = "submit_patch"
            print(f"\n{C_GREEN}submit_patch called - ending session.{C_RESET}")
            break
        if sb.context_overflow:
            end_reason = "context_overflow"
            print(f"\n{C_RED}context window exceeded - ending session.{C_RESET}")
            break
        blocked = sb.budget_exhausted()
        if blocked:
            end_reason = blocked.replace(" ", "_")
            print(f"\n{C_YELLOW}{blocked} - ending session.{C_RESET}")
            break
        if out["had_tool_call"]:
            nudges = 0
        else:
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
        print(f"\n{C_YELLOW}[nudge {max(nudges, 1)}/3]{C_RESET} {nudge[:90]}...")
        root.add_user(nudge)

    patch = sb.submitted_patch
    if not patch:
        patch = sb.live_diff()
        if patch.strip():
            print(f"{C_YELLOW}No submit_patch; recovered {len(patch)} B from the working tree.{C_RESET}")
    limit = float(sb.budget["max_time_minutes"]) * 60.0
    return {
        "turns": sb.llm_calls,
        "invocations": invocations,
        "tool_calls": sb.tool_calls_used,
        "elapsed_seconds": round(sb.elapsed, 1),
        "wall_seconds": round(time.time() - wall0, 1),
        "simulated_clock": bool(sb.sim),
        "overshoot_seconds": round(max(0.0, sb.elapsed - limit), 1),
        "patch_submitted": sb.patch_submitted,
        "end_reason": end_reason,
        "nudges": total_nudges,
        "edits_landed": sb.edits_landed,
        "first_edit_turn": sb.first_edit_call,
        "prompt_tokens_total": sb.prompt_tokens_total,
        "output_tokens_total": sb.output_tokens_total,
        "prompt_tokens_max": sb.prompt_tokens_max,
        "compactions": sb.compactions,
        "phase_stats": sb.phase_stats,
        "agent_env": sb.agent_env_key,
        "tool_uses": dict(sb.tool_uses),
        "tool_errors": dict(sb.tool_errors),
        "failed_calls": sb.failed_calls,
        "trace": sb.trace,
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
    return OpenAIBackend(
        model,
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        api_base=api_base or os.environ.get("SWE_API_BASE", DEFAULT_API_BASE),
    )


def run_task(
    sub: Submission,
    task: Dict[str, Any],
    snapshot: Path,
    backend: Backend,
    verify: bool = False,
    verbose: bool = True,
    keep_sandbox: bool = False,
    sim: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Phase 1 (+ optional Phase 2) for one task. Shared by --auto and run_experiment.py."""
    tmp = tempfile.mkdtemp(prefix=f"swegemma_{task['instance_id']}_")
    workspace = Path(tmp) / "workspace"
    try:
        extract_snapshot_to_dir(snapshot, workspace)
        agent_env = os.environ.get("SWE_AGENT_ENV") or os.environ.get("SWE_TEST_ENV", "auto")
        sb = Sandbox(workspace, task, dict(sub.budget), sim=sim, agent_env=agent_env)
        root = RootRunner(sub.root, sb, backend, verbose=verbose)
        root.add_user(build_initial_prompt(task, sb, sub.root))
        result = run_autonomous(root, sb)
        result["instance_id"] = task["instance_id"]
        result["repo"] = task["repo"]
        result["patch_size"] = len(result["patch"])
        if verify:
            if result["patch"].strip():
                v = verify_agent_patch(task, snapshot, result["patch"])
                result["resolved"] = bool(v.get("resolved"))
                result["pytest_exit_code"] = v.get("exit_code")
                result["verify_error"] = v.get("error")
                result["verify_stdout"] = (v.get("stdout") or "")[-1500:]
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


IMPORT_CRASH_MARKERS = (
    "NameError", "SyntaxError", "IndentationError", "ImportError", "ModuleNotFoundError",
    "error during collection", "errors during collection", "TypeError: non-default argument",
)


def classify_failure(r: Dict[str, Any]) -> str:
    """Bucket a non-resolved task so a sweep says *why* it failed, not just that it did."""
    if r.get("resolved"):
        return "resolved"
    if not r.get("patch_size"):
        if not r.get("edits_landed"):
            return "no_edit_attempted"
        return "edits_made_but_no_diff"
    if r.get("patch_size", 0) > 1_000_000:
        return "patch_polluted"
    err = str(r.get("verify_error") or "")
    if "apply" in err.lower():
        return "patch_did_not_apply"
    out = str(r.get("verify_stdout") or "")
    # A collection error means the patch broke importing the package: a different defect from a
    # wrong fix, and one a compile/import check before submitting would have caught.
    if r.get("pytest_exit_code") == 2 or "during collection" in out:
        if "ModuleNotFoundError: No module named" in out or "package to be installed" in out:
            return "test_env_broken"
        if any(m in out for m in IMPORT_CRASH_MARKERS):
            return "patch_breaks_import"
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


def repl(root: "RootRunner", sb: Sandbox, task: Dict[str, Any], snapshot: Path,
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
            print(json.dumps(sb.call("get_status", {}, {}), indent=2))
            continue
        if low == "/tree":
            print(sub.describe())
            continue
        if low == "/prompt":
            print(initial_prompt)
            continue
        if low == "/tools":
            for a in root.sessions():
                print(f"{a.spec.name}: {', '.join(sorted(a.tools))}  "
                      f"(~{a.overhead_tokens()} tokens per request)")
            continue
        if low.startswith("/run "):
            res = sb.call("run_command", {"command": line[5:]}, {})
            print(res.get("stdout") or res.get("details", {}).get("stdout", ""))
            err = res.get("stderr") or res.get("details", {}).get("stderr", "")
            if err:
                print(f"{C_RED}{err}{C_RESET}")
            print(f"exit={res.get('exit_code', res.get('details', {}).get('exit_code'))}")
            continue
        if low.startswith("/cat "):
            res = sb.call("read_file", {"filepath": line[5:].strip()}, {})
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
            root.add_user(initial_prompt)
            print(json.dumps(
                {k: v for k, v in run_autonomous(root, sb).items()
                 if k not in ("patch", "trace")}, indent=2))
            continue

        root.add_user(line)
        try:
            root.invoke()
        except StateKeyError as e:
            print(f"{C_RED}{e}{C_RESET}")


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
    ap.add_argument("--api-base", default=os.environ.get("SWE_API_BASE", DEFAULT_API_BASE),
                    help="OpenAI-compatible endpoint for --backend openai (env SWE_API_BASE)")
    ap.add_argument("--probe-endpoint", action="store_true",
                    help="check --api-base: list models, one tool call, measured tokens/second")
    ap.add_argument("--sim", choices=sorted(SIM_PRESETS),
                    help="run the task clock at assumed Gemma speeds instead of API speed")
    ap.add_argument("--sim-prefill-tps", type=float, help="prompt tokens/second for the simulated clock")
    ap.add_argument("--sim-decode-tps", type=float, help="output tokens/second for the simulated clock")
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

    if args.probe_endpoint:
        be = OpenAIBackend(args.model or "", api_key=os.environ.get("OPENAI_API_KEY", ""),
                           api_base=args.api_base)
        if not be.model:
            req = urllib.request.Request(f"{be.api_base}/models")
            with urllib.request.urlopen(req, timeout=30) as resp:
                be.model = json.loads(resp.read().decode("utf-8"))["data"][0]["id"]
        info = be.probe()
        print(json.dumps(info, indent=2))
        if info.get("decode_tps") and info.get("prefill_tps"):
            print(f"\nUse these for the simulated clock:\n  --sim-prefill-tps "
                  f"{info['prefill_tps']} --sim-decode-tps {info['decode_tps']}")
        if not info.get("tool_call_parsed"):
            print(f"{C_RED}The endpoint did not return a parsed tool call. Check that vLLM runs "
                  f"with --enable-auto-tool-choice --tool-call-parser gemma4.{C_RESET}")
        return

    sim = make_sim(args)
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
            verify=args.verify, keep_sandbox=args.keep_sandbox, sim=sim,
        )
        patch = result.pop("patch")
        out_dir = Path(args.results_dir) / task["instance_id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "trace.json").write_text(
            json.dumps(result.pop("trace", []), indent=1), encoding="utf-8")
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
    sb = Sandbox(workspace, task, sub.budget, sim=sim,
                 agent_env="host" if args.dry_run else os.environ.get("SWE_TEST_ENV", "auto"))
    root = RootRunner(sub.root, sb, backend)
    initial_prompt = build_initial_prompt(task, sb, sub.root)

    try:
        if args.dry_run:
            # Fill the keys that earlier agents produce so every instruction can be rendered.
            for a in sub.root.all_agents():
                if a.output_key:
                    sb.state.setdefault(a.output_key, f"<{a.output_key} from {a.name}>")
            for a in root.sessions():
                print(f"\n{C_BOLD}--- {a.spec.name}: {', '.join(sorted(a.tools))} ---{C_RESET}")
                si = a.system_instruction()
                print(f"instruction {len(si)} chars; ~{a.overhead_tokens()} tokens sent on "
                      f"every request (instruction + tool schemas)")
                print(si[:1200] + ("\n..." if len(si) > 1200 else ""))
            print(f"\n{C_BOLD}--- initial user prompt ({len(initial_prompt)} chars, "
                  f"~{estimate_tokens(initial_prompt)} tokens) ---{C_RESET}")
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
