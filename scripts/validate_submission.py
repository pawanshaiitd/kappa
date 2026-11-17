"""Submission Validator for swegemma / adk-submission.

Validates that a submission directory complies with competition harness constraints:
1. Root config discovery (agent.yaml)
2. Allowed file extensions
3. Size limits (< 3 GiB total unpacked)
4. Single base model declaration across all agents and sub-agents
5. Inclusion traversal checks (!include)
6. LoRA adapter structure
"""

from __future__ import annotations

import os
import re
import sys
import yaml
from pathlib import Path
from typing import Set, Dict, Any, List

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ALLOWED_MODEL_NAMES = {
    "gemma-4-31b-it-qat-w4a16-ct",
    "gemma-4-12b-it-qat-w4a16-ct",
    "gemma-4-e4b-it-qat-w4a16-ct",
    "gemma-4-e2b-it-qat-w4a16-ct",
    # Local CLI aliases
    "gemma-4-31b-it",
    "gemma-4-12b-it",
    "gemma-4-e4b-it",
    "gemma-4-e2b-it",
}

ALLOWED_EXTENSIONS = {
    ".yaml", ".yml", ".md", ".txt", ".py", ".json", ".safetensors"
}

MAX_TOTAL_SIZE_BYTES = 3 * 1024 * 1024 * 1024  # 3 GiB

# Time safety. Tasks run one after another, so the per-task cap decides whether the run finishes.
# Worst case, every task runs to the cap:
#   startup + PLAN_TASKS * (max_time_minutes + overshoot + SETUP_MINUTES)  <=  RUN_LIMIT_MINUTES
# overshoot: the cap is checked only between tool calls, so the last reply can run over by up to
# max_output_tokens / PLAN_DECODE_TPS seconds.
# Numbers (2026-11-17): the hidden set is about 124 tasks. Accepted submissions ran up to 24 h
# including queueing; submission #2 was killed at 18 h of runtime. The guard requires the worst
# case to fit in 24 h and reports where it stands against 18 h. PLAN_DECODE_TPS is the
# single-stream decode speed measured on the 31B vLLM endpoint (10.6 tok/s).
RUN_LIMIT_MINUTES = 24 * 60
OBSERVED_KILL_MINUTES = 18 * 60
PLAN_TASKS = 130
SETUP_MINUTES = 1.0
STARTUP_MINUTES = 35.0
PLAN_DECODE_TPS = 10.6
MAX_SAFE_OUTPUT_TOKENS = 2048


def max_output_tokens_of(sub_path: Path) -> int:
    """Largest max_output_tokens declared anywhere in the submission (ADK default 16384)."""
    found = [int(m.group(1)) for y in sub_path.rglob("*.yaml")
             for m in re.finditer(r"(?m)^\s*max_output_tokens:\s*(\d+)", y.read_text(encoding="utf-8"))]
    return max(found) if found else 16384


def worst_case_hours(sub_path: Path, minutes: float) -> float:
    """Run length if every one of PLAN_TASKS tasks runs to the time cap."""
    overshoot = max_output_tokens_of(sub_path) / PLAN_DECODE_TPS / 60
    return (STARTUP_MINUTES + PLAN_TASKS * (minutes + overshoot + SETUP_MINUTES)) / 60


def time_safety_problems(sub_path: Path) -> List[str]:
    """Reasons this submission could run past the notebook limit or fail to start."""
    problems: List[str] = []
    budget = {"max_time_minutes": 60.0}
    ec = sub_path / "eval_config.yaml"
    if ec.exists():
        data = yaml.safe_load(ec.read_text(encoding="utf-8")) or {}
        budget.update(data.get("evaluation") or {})
    else:
        problems.append("no eval_config.yaml: the default is 60 minutes per task")
    minutes = float(budget["max_time_minutes"])
    hours = worst_case_hours(sub_path, minutes)
    if hours * 60 > RUN_LIMIT_MINUTES:
        problems.append(
            f"max_time_minutes={minutes:g}: if all {PLAN_TASKS} tasks reach the cap the run takes "
            f"{hours:.1f} h (cap + last-reply overshoot + {SETUP_MINUTES:g} min setup per task, "
            f"{STARTUP_MINUTES:g} min start-up), over the {RUN_LIMIT_MINUTES // 60} h limit."
        )
    for y in sorted(sub_path.rglob("*.yaml")):
        txt = y.read_text(encoding="utf-8")
        for m in re.finditer(r"(?m)^\s*max_output_tokens:\s*(\d+)", txt):
            if int(m.group(1)) > MAX_SAFE_OUTPUT_TOKENS:
                problems.append(
                    f"{y.relative_to(sub_path)}: max_output_tokens={m.group(1)} lets one "
                    f"generation overrun the time cap by minutes; keep it <= {MAX_SAFE_OUTPUT_TOKENS}"
                )
        if re.search(r"(?m)^\s*thinking_level:", txt):
            problems.append(f"{y.relative_to(sub_path)}: thinking_level is dropped by vLLM")
    adapters = sub_path / "adapters"
    if adapters.is_dir() and any(d.is_dir() for d in adapters.iterdir()):
        problems.append("adapters/ present: this switches LoRA on in vLLM; ship only a tested adapter")
    return problems


def normalize_model_name(name: str) -> str:
    for prefix in ("openai/", "google/", "hosted_vllm/", "custom/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def custom_yaml_loader(submission_root: Path):
    """YAML loader that handles !include without executing arbitrary code."""
    class SafeIncludeLoader(yaml.SafeLoader):
        pass

    def include_constructor(loader, node):
        filename = loader.construct_scalar(node)
        target_path = (Path(loader.name).parent / filename).resolve()
        # Path traversal guard
        if not str(target_path).startswith(str(submission_root.resolve())):
            raise ValueError(f"Path traversal detected: {filename} escapes {submission_root}")
        if not target_path.exists():
            raise FileNotFoundError(f"Included file does not exist: {target_path}")

        if target_path.suffix in (".md", ".txt"):
            return target_path.read_text(encoding="utf-8")
        elif target_path.suffix in (".yaml", ".yml"):
            with open(target_path, "r", encoding="utf-8") as f:
                sub_loader = SafeIncludeLoader(f)
                sub_loader.name = str(target_path)
                return sub_loader.get_data()
        return str(target_path)

    SafeIncludeLoader.add_constructor("!include", include_constructor)
    return SafeIncludeLoader


def validate_submission_dir(submission_dir: str | Path) -> Dict[str, Any]:
    sub_path = Path(submission_dir).resolve()
    if not sub_path.is_dir():
        return {"valid": False, "error": f"{sub_path} is not a directory"}

    # 1. Root config
    root_candidates = [
        sub_path / name
        for name in ("agent.yaml", "agent.yml", "root_agent.yaml", "root_agent.yml")
        if (sub_path / name).exists()
    ]
    if len(root_candidates) == 0:
        return {"valid": False, "error": "Missing root config (agent.yaml)"}
    if len(root_candidates) > 1:
        return {"valid": False, "error": f"Multiple root configs found: {root_candidates}"}
    root_config = root_candidates[0]

    # 2. File size and extensions
    total_size = 0
    disallowed_files = []
    for root, dirs, files in os.walk(sub_path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            fp = Path(root) / f
            total_size += fp.stat().st_size
            if fp.suffix.lower() not in ALLOWED_EXTENSIONS:
                disallowed_files.append(str(fp.relative_to(sub_path)))

    if total_size > MAX_TOTAL_SIZE_BYTES:
        return {"valid": False, "error": f"Submission unpacked size ({total_size} bytes) exceeds 3 GiB limit."}
    if disallowed_files:
        return {"valid": False, "error": f"Disallowed file extensions found: {disallowed_files}"}

    # 3. Parse YAMLs and discover declared models
    loader_cls = custom_yaml_loader(sub_path)
    models_found: Set[str] = set()

    def inspect_agent_dict(d: Any, source_file: str):
        if not isinstance(d, dict):
            return
        if "model" in d and isinstance(d["model"], str):
            norm = normalize_model_name(d["model"])
            models_found.add(norm)
        # Check sub_agents
        for sa in d.get("sub_agents", []):
            if isinstance(sa, dict) and "config_path" in sa:
                sa_path = (Path(source_file).parent / sa["config_path"]).resolve()
                if sa_path.exists():
                    load_and_inspect_yaml(sa_path)
            elif isinstance(sa, dict):
                inspect_agent_dict(sa, source_file)
        # Check tools agent_tool
        for tool in d.get("tools", []):
            if isinstance(tool, dict) and "agent_tool" in tool:
                at_conf = tool["agent_tool"]
                if isinstance(at_conf, dict) and "config_path" in at_conf:
                    at_path = (Path(source_file).parent / at_conf["config_path"]).resolve()
                    if at_path.exists():
                        load_and_inspect_yaml(at_path)

    def load_and_inspect_yaml(yaml_path: Path):
        with open(yaml_path, "r", encoding="utf-8") as f:
            sub_loader = loader_cls(f)
            sub_loader.name = str(yaml_path)
            data = sub_loader.get_data()
            inspect_agent_dict(data, str(yaml_path))

    try:
        load_and_inspect_yaml(root_config)
    except Exception as e:
        return {"valid": False, "error": f"Failed to parse agent configs: {e}"}

    # The scorer also reads agent YAMLs that nothing references.
    for y in sorted(sub_path.rglob("*.yaml")) + sorted(sub_path.rglob("*.yml")):
        for m in re.finditer(r"(?m)^model:\s*['\"]?([^\s'\"#]+)", y.read_text(encoding="utf-8")):
            models_found.add(normalize_model_name(m.group(1)))

    if len(models_found) == 0:
        return {"valid": False, "error": "No model declared in submission."}
    if len(models_found) > 1:
        return {"valid": False, "error": f"Single-model rule violated! Found multiple models: {models_found}"}

    declared_model = list(models_found)[0]
    if declared_model not in ALLOWED_MODEL_NAMES:
        return {
            "valid": False,
            "error": f"Model '{declared_model}' is not in ALLOWED_MODEL_NAMES: {ALLOWED_MODEL_NAMES}",
        }

    unsafe = time_safety_problems(sub_path)
    if unsafe:
        return {"valid": False, "error": "Time/startup safety: " + " | ".join(unsafe)}

    # Structure, tools and session-state keys, checked by the same compiler the local harness uses.
    try:
        from scripts.submission_harness import Submission

        compile_problems = Submission(sub_path).validate()
    except Exception as e:  # noqa: BLE001
        compile_problems = [f"does not compile: {e}"]
    if compile_problems:
        return {"valid": False, "error": "Compile: " + " | ".join(compile_problems)}

    return {
        "valid": True,
        "submission_dir": str(sub_path),
        "root_config": root_config.name,
        "declared_model": declared_model,
        "total_size_mb": round(total_size / (1024 * 1024), 2),
    }


def main():
    targets = sys.argv[1:] or ["sample_submission", "sample_submission_lean"]
    failed = False
    for target in targets:
        print(f"--- Validating {target} ---")
        res = validate_submission_dir(target)
        if res["valid"]:
            print(f"  [PASS] Valid submission!")
            print(f"  Declared Model: {res['declared_model']}")
            print(f"  Unpacked Size:  {res['total_size_mb']} MB")
            ec = Path(target) / "eval_config.yaml"
            ev = ((yaml.safe_load(ec.read_text(encoding="utf-8")) or {}).get("evaluation") or {}) if ec.exists() else {}
            minutes = float(ev.get("max_time_minutes", 60.0))
            hours = worst_case_hours(Path(target), minutes)
            flag = "within" if hours * 60 <= OBSERVED_KILL_MINUTES else "OVER"
            print(f"  Worst case:     {hours:.1f} h if all {PLAN_TASKS} tasks hit the {minutes:g}-min cap "
                  f"({flag} the 18 h runtime at which submission #2 was killed; limit "
                  f"{RUN_LIMIT_MINUTES // 60} h)")
        else:
            failed = True
            print(f"  [FAIL] {res['error']}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
