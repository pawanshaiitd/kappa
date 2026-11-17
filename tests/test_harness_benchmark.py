"""Submission compliance tests.

1. Both shippable submissions validate, declare one allowed model and pass the time-safety guard.
2. The shipped pipeline has the intended structure and session-state hand-off.
3. The 3-tier edit matcher and the per-task test environment resolver behave as documented.
"""

import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import json
import os
import re
import pytest
import yaml

from scripts.validate_submission import validate_submission_dir, custom_yaml_loader
from scripts.harness_tools import apply_replacement_3tier, FileEditError

SUBMISSION_DIR = REPO_ROOT / "sample_submission"
# Both shippable variants: the three-step pipeline and the single lean agent.
SUBMISSION_DIRS = [REPO_ROOT / "sample_submission", REPO_ROOT / "sample_submission_lean"]
DECLARED_MODEL = "gemma-4-31b-it-qat-w4a16-ct"


@pytest.mark.parametrize("sub_dir", SUBMISSION_DIRS, ids=lambda p: p.name)
def test_submission_compliance_benchmark(sub_dir):
    """Each shippable submission satisfies the competition rules and the time-safety guard."""
    res = validate_submission_dir(sub_dir)
    assert res["valid"] is True, f"Submission validation failed: {res.get('error')}"
    assert res["declared_model"] == DECLARED_MODEL
    assert res["total_size_mb"] < 3072.0  # < 3 GiB


@pytest.mark.parametrize("sub_dir", SUBMISSION_DIRS, ids=lambda p: p.name)
def test_only_live_files_are_shipped(sub_dir):
    """Everything in the zip is reachable from agent.yaml; nothing else rides along.

    Unreferenced agent YAMLs are read by the scorer's model discovery, adapters/ switches LoRA
    on in vLLM, and docs only add size. Keep the directory to config, prompts and eval budget.
    """
    allowed_top = {"agent.yaml", "eval_config.yaml", "configs", "prompts",
                   "locator.yaml", "reporter.yaml", "fixer.yaml", "verifier.yaml"}
    present = {p.name for p in sub_dir.iterdir() if not p.name.startswith(".")}
    assert present <= allowed_top, f"unexpected entries in {sub_dir.name}: {sorted(present - allowed_top)}"
    assert not (sub_dir / "adapters").exists()


def test_time_safety_guard_rejects_a_slow_submission(tmp_path):
    """The mistake behind the 18 h timeout must not validate again."""
    from scripts.validate_submission import time_safety_problems

    (tmp_path / "eval_config.yaml").write_text(
        "evaluation:\n  max_time_minutes: 60\n", encoding="utf-8")
    (tmp_path / "sampling.yaml").write_text("max_output_tokens: 6144\n", encoding="utf-8")
    problems = time_safety_problems(tmp_path)
    assert any("max_time_minutes" in p for p in problems)
    assert any("max_output_tokens" in p for p in problems)
    for sub_dir in SUBMISSION_DIRS:
        assert time_safety_problems(sub_dir) == []


def test_pipeline_structure():
    """The shipped tree: locator -> reporter -> fixer -> verifier with state hand-off."""
    from scripts.submission_harness import Submission

    sub = Submission(SUBMISSION_DIR)
    assert sub.validate() == []
    root = sub.root
    assert root.agent_class == "SequentialAgent"
    assert [a.name for a in root.sub_agents] == ["locator", "reporter", "fixer", "verifier"]
    locator, reporter, fixer, verifier = root.sub_agents
    assert reporter.output_key == "location" and fixer.output_key == "fix_summary"
    # The reporter has no tools, so it always ends after one reply: the hand-off is forced.
    assert reporter.tool_names == [] and reporter.include_contents != "none"
    assert reporter.gen_cfg["thinking_config"]["thinking_budget"] == 0
    assert fixer.include_contents == "none" and verifier.include_contents == "none"
    # Only the last step can end the session, and the read-only step cannot edit.
    assert "submit_patch" in verifier.tool_names
    assert "submit_patch" not in locator.tool_names + fixer.tool_names + reporter.tool_names
    assert not {"edit_file", "write_file"} & set(locator.tool_names)
    for a in root.sub_agents:
        assert len(a.tool_names) <= 5, f"{a.name} declares too many tools"
        assert len(a.instruction) <= 4000, f"{a.name} instruction is {len(a.instruction)} chars"
    assert ("location", True) in fixer.state_keys_used()
    assert ("problem_description", False) in fixer.state_keys_used()


def test_matcher_benchmark_suite():
    """Rigorous benchmark for 3-tier file matcher handling difficult code changes."""
    # 1. Multi-line function replacement with indentation difference
    source = """class Service:
    def process(self, data):
        # Step 1: validate
        if not data:
            return None
        # Step 2: compute
        res = data.get("value") * 2
        return res
"""
    old_hunk = """    if not data:
        return None
    # Step 2: compute
    res = data.get("value") * 2"""

    new_hunk = """    if not data:
        raise ValueError("Missing data")
    # Step 2: compute with offset
    res = (data.get("value") + 1) * 2"""

    updated, strategy, count = apply_replacement_3tier(source, old_hunk, new_hunk)
    assert count == 1
    assert "raise ValueError" in updated
    assert "res = (data.get(\"value\") + 1) * 2" in updated

    # 2. Ambiguity check: duplicate string must raise FileEditError
    dup_source = "pass\npass\npass"
    with pytest.raises(FileEditError):
        apply_replacement_3tier(dup_source, "pass", "return True", allow_multiple=False)


def test_no_parent_traversal_in_includes():
    """`!include ../x.md` has a '..' component, which the submission loader rejects."""
    for y in sorted(p for d in SUBMISSION_DIRS for p in d.rglob("*.yaml")):
        for i, line in enumerate(y.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # a comment mentioning !include is not a directive
            if "!include" in line:
                target = line.split("!include", 1)[1].strip()
                assert ".." not in target.split("/"), f"{y.name}:{i} traversal in !include: {target}"
                assert not target.startswith("/"), f"{y.name}:{i} absolute !include: {target}"


@pytest.mark.parametrize("sub_dir", SUBMISSION_DIRS, ids=lambda p: p.name)
def test_single_declared_model_across_tree(sub_dir):
    """Every YAML in the directory, referenced or not, declares the same allowed model."""
    models = set()
    for y in sorted(sub_dir.rglob("*.yaml")):
        for line in y.read_text(encoding="utf-8").splitlines():
            if line.startswith("model:"):
                models.add(line.split(":", 1)[1].strip().split("/")[-1])
    assert models == {DECLARED_MODEL}, f"declared models: {sorted(models)}"


@pytest.mark.parametrize("sub_dir", SUBMISSION_DIRS, ids=lambda p: p.name)
def test_prompts_have_no_stray_braces(sub_dir):
    """ADK templates {name} in instructions; an unknown key aborts the task with KeyError."""
    known = {"problem_description", "hints", "location", "fix_summary"}
    for md in sorted((sub_dir / "prompts").glob("*.md")):
        for name in re.findall(r"\{([^{}\n]*)\}", md.read_text(encoding="utf-8")):
            assert name.rstrip("?") in known, f"{md.name}: {{{name}}} is not a session-state key"


def test_real_adk_accepts_the_agent_tree():
    """Build the tree with the real ADK classes when google-adk is installed (it is on the server)."""
    pytest.importorskip("google.adk")
    from google.adk.agents import LlmAgent, SequentialAgent

    from scripts.submission_harness import Submission

    def build(spec):
        if spec.agent_class == "SequentialAgent":
            return SequentialAgent(name=spec.name, sub_agents=[build(s) for s in spec.sub_agents])
        kwargs = dict(name=spec.name, model=spec.model, instruction=spec.instruction,
                      description=spec.description, include_contents=spec.include_contents)
        if spec.output_key:
            kwargs["output_key"] = spec.output_key
        return LlmAgent(**kwargs)

    for sub_dir in SUBMISSION_DIRS:
        agent = build(Submission(sub_dir).root)
        assert agent.name


# --- Per-task test environment resolution (scripts/test_envs.py) ---------------------------

def _write_snapshot_like(tmp_path, pyproject: str = "", files: dict | None = None):
    """Build a fake extracted snapshot on disk. `files` maps real filenames to contents."""
    if pyproject:
        (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    for name, body in (files or {}).items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_resolve_requirements_never_installs_package_under_test(tmp_path):
    """An installed copy of the repo would shadow the patched workspace on sys.path."""
    from scripts.test_envs import resolve_requirements

    reqs = resolve_requirements(
        _write_snapshot_like(
            tmp_path,
            pyproject='[project]\nname = "mylib"\ndependencies = ["urllib3>=1.26,<3"]\n',
            files={"requirements-dev.txt": "-e .[socks]\npytest>=7\nmylib-helper\n"},
        ),
        repo="someone/mylib",
    )
    names = {r.split(">")[0].split("[")[0].strip().lower() for r in reqs}
    assert "mylib" not in names
    assert "urllib3>=1.26,<3" in reqs


def test_resolve_requirements_preserves_pins_and_expands_extras(tmp_path):
    """The starlette-style ceiling is the whole point; `-e .[all]` extras must survive too."""
    from scripts.test_envs import resolve_requirements

    reqs = resolve_requirements(
        _write_snapshot_like(
            tmp_path,
            pyproject=(
                '[project]\nname = "web"\n'
                'dependencies = ["starlette>=0.40.0,<0.51.0"]\n'
                '[project.optional-dependencies]\n'
                'all = ["python-multipart >=0.0.18", "orjson >=3.2.1"]\n'
            ),
            files={"requirements-tests.txt": "-e .[all]\npytest >=7.1.3,<9.0.0\nmypy ==1.14.1\n"},
        ),
        repo="x/web",
    )
    assert "starlette>=0.40.0,<0.51.0" in reqs
    assert "python-multipart >=0.0.18" in reqs
    assert "pytest >=7.1.3,<9.0.0" in reqs, "the repo's own pytest pin must win over a bare default"
    assert not any(r.startswith("mypy") for r in reqs), "type checkers are not test dependencies"


def test_env_key_is_order_independent_and_distinguishing():
    from scripts.test_envs import env_key

    assert env_key(["b", "a"]) == env_key(["a", "b"])
    assert env_key(["a"]) != env_key(["a", "c"])


def test_pythonpath_includes_src_layout(tmp_path):
    """src-layout repos are not importable from the workspace root alone."""
    from scripts.run_gemini_benchmark import _pythonpath_for

    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    assert str(tmp_path / "src") in _pythonpath_for(tmp_path).split(os.pathsep)

    flat = tmp_path / "flat"
    (flat / "pkg").mkdir(parents=True)
    (flat / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    assert _pythonpath_for(flat) == str(flat)


def test_generated_pytest_ini_defers_to_repo_config(tmp_path):
    """Writing pytest.ini unconditionally discards the repo's testpaths and required plugins."""
    from scripts.run_gemini_benchmark import _repo_configures_pytest

    assert not _repo_configures_pytest(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    assert _repo_configures_pytest(tmp_path)
