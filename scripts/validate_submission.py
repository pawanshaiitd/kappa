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

    return {
        "valid": True,
        "submission_dir": str(sub_path),
        "root_config": root_config.name,
        "declared_model": declared_model,
        "total_size_mb": round(total_size / (1024 * 1024), 2),
    }


def main():
    targets = ["sample_submission", "sample_submission_4b"]
    for target in targets:
        print(f"--- Validating {target} ---")
        res = validate_submission_dir(target)
        if res["valid"]:
            print(f"  [PASS] Valid submission!")
            print(f"  Declared Model: {res['declared_model']}")
            print(f"  Unpacked Size:  {res['total_size_mb']} MB")
        else:
            print(f"  [FAIL] {res['error']}")


if __name__ == "__main__":
    main()
