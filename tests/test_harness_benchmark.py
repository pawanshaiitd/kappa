"""Automated Benchmark Suite for the Mature Coding Harness.

Evaluates the coding harness across:
1. Declarative Submission Compliance
2. 5 Crucial Core Components (Context, Multi-Agent, Security, Verification, Skills)
3. Memory & ASR Schema Integrity
4. 3-Tier Surgical Matcher Precision & Error Handling
5. Real SWE-bench Task Symbol Navigation
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


def test_submission_compliance_benchmark():
    """Validates that sample_submission satisfies all competition harness rules."""
    res = validate_submission_dir(SUBMISSION_DIR)
    assert res["valid"] is True, f"Submission validation failed: {res.get('error')}"
    assert res["declared_model"] == "gemma-4-31b-it-qat-w4a16-ct"
    assert res["total_size_mb"] < 3072.0  # < 3 GiB


def test_core_components_presence():
    """Validates that all 5 crucial core components exist with proper structure."""
    expected_dirs = ["rules", "tools", "memory", "asr", "skills", "prompts", "sub_agents", "configs"]
    for d in expected_dirs:
        p = SUBMISSION_DIR / d
        assert p.is_dir(), f"Missing core component directory: {d}"

    # Rules
    assert (SUBMISSION_DIR / "rules" / "python_standards.md").exists()
    assert (SUBMISSION_DIR / "rules" / "test_isolation.md").exists()
    assert (SUBMISSION_DIR / "rules" / "file_editing.md").exists()
    assert (SUBMISSION_DIR / "rules" / "patch_hygiene.md").exists()

    # Tools
    assert (SUBMISSION_DIR / "tools" / "README.md").exists()
    assert (SUBMISSION_DIR / "tools" / "tool_schemas.json").exists()
    assert (SUBMISSION_DIR / "tools" / "execution" / "run_command.md").exists()
    assert (SUBMISSION_DIR / "tools" / "execution" / "submit_patch.md").exists()
    assert (SUBMISSION_DIR / "tools" / "workspace" / "read_file.md").exists()
    assert (SUBMISSION_DIR / "tools" / "workspace" / "edit_file.md").exists()
    assert (SUBMISSION_DIR / "tools" / "workspace" / "write_file.md").exists()
    assert (SUBMISSION_DIR / "tools" / "graph" / "code_intelligence.md").exists()

    # Memory
    assert (SUBMISSION_DIR / "memory" / "README.md").exists()
    assert (SUBMISSION_DIR / "memory" / "session_state.md").exists()
    assert (SUBMISSION_DIR / "memory" / "trajectory_cache.json").exists()
    assert (SUBMISSION_DIR / "memory" / "memory_schema.json").exists()

    # ASR
    assert (SUBMISSION_DIR / "asr" / "README.md").exists()
    assert (SUBMISSION_DIR / "asr" / "audio_spec.md").exists()
    assert (SUBMISSION_DIR / "asr" / "transcription_pipeline.md").exists()
    assert (SUBMISSION_DIR / "asr" / "audio_schema.json").exists()

    # Skills: ADK requires kebab-case names that equal the directory name.
    for skill_name in ["repo-navigation", "code-intelligence", "precision-editing", "test-verification"]:
        sk_path = SUBMISSION_DIR / "skills" / skill_name
        assert (sk_path / "SKILL.md").exists(), f"Missing skill: {skill_name}"

    # adapters/ must be absent: any subdirectory there switches LoRA on in vLLM, and an
    # untrained placeholder adapter can stop the inference server from starting at all.
    adapters = SUBMISSION_DIR / "adapters"
    if adapters.is_dir():
        real = [d for d in adapters.iterdir() if d.is_dir()]
        assert not real, f"Untrained/unverified adapters present: {[d.name for d in real]}"


def test_tool_schemas_validity():
    """Validates that tool_schemas.json is valid JSON and defines all 9 tools."""
    schemas_file = SUBMISSION_DIR / "tools" / "tool_schemas.json"
    data = json.loads(schemas_file.read_text(encoding="utf-8"))
    required_tools = [
        "run_command", "read_file", "edit_file", "write_file",
        "submit_patch", "get_status", "search_similar_code",
        "get_code_neighbors", "get_code_subgraph"
    ]
    for tool in required_tools:
        assert tool in data, f"Missing tool schema: {tool}"
        assert data[tool]["type"] == "object"


def test_memory_and_asr_json_validity():
    """Validates memory and ASR json configurations."""
    mem_cache = json.loads((SUBMISSION_DIR / "memory" / "trajectory_cache.json").read_text(encoding="utf-8"))
    assert "distilled_lessons" in mem_cache
    assert len(mem_cache["distilled_lessons"]) >= 4

    mem_schema = json.loads((SUBMISSION_DIR / "memory" / "memory_schema.json").read_text(encoding="utf-8"))
    assert mem_schema["type"] == "object"

    asr_schema = json.loads((SUBMISSION_DIR / "asr" / "audio_schema.json").read_text(encoding="utf-8"))
    assert asr_schema["type"] == "object"


def test_agent_yaml_structure_and_skills():
    """Validates agent.yaml root config and sub-agent bindings."""
    loader_cls = custom_yaml_loader(SUBMISSION_DIR)
    agent_file = SUBMISSION_DIR / "agent.yaml"
    with open(agent_file, "r", encoding="utf-8") as f:
        loader = loader_cls(f)
        loader.name = str(agent_file)
        agent_yaml = loader.get_data()

    assert agent_yaml["name"] == "mkpro_coordinator"
    assert agent_yaml["model"] == "gemma-4-31b-it-qat-w4a16-ct"
    assert len(agent_yaml["skills"]) == 4

    # Check sub-agents exist
    for sub in agent_yaml.get("tools", []):
        if isinstance(sub, dict) and "agent_tool" in sub:
            cfg = sub["agent_tool"]["config_path"]
            sub_file = SUBMISSION_DIR / cfg
            assert sub_file.exists(), f"Declared sub-agent file not found: {cfg}"


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


def test_skills_load_with_adk_rules():
    """Skill frontmatter must satisfy ADK: kebab-case name == directory name, and a description.

    ADK rejects snake_case skill names unless the SNAKE_CASE_SKILL_NAME feature flag is on,
    and a rejected skill fails submission compilation, which zeroes every task.
    """
    import yaml

    skills_dir = SUBMISSION_DIR / "skills"
    found = sorted(d for d in skills_dir.iterdir() if d.is_dir())
    assert found, "no skills found"
    for d in found:
        md = d / "SKILL.md"
        assert md.exists(), f"{d.name}: no SKILL.md"
        text = md.read_text(encoding="utf-8")
        assert text.startswith("---"), f"{d.name}: no YAML frontmatter"
        fm = yaml.safe_load(text.split("---", 2)[1]) or {}
        name = fm.get("name", "")
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name), f"{d.name}: name {name!r} not kebab-case"
        assert name == d.name, f"directory {d.name!r} != frontmatter name {name!r}"
        assert fm.get("description"), f"{d.name}: no description"
        assert not (d / "resources").is_dir(), f"{d.name}: ADK reads references/, not resources/"


def test_no_parent_traversal_in_includes():
    """`!include ../x.md` has a '..' component, which the submission loader rejects."""
    for y in sorted(SUBMISSION_DIR.rglob("*.yaml")):
        for i, line in enumerate(y.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # a comment mentioning !include is not a directive
            if "!include" in line:
                target = line.split("!include", 1)[1].strip()
                assert ".." not in target.split("/"), f"{y.name}:{i} traversal in !include: {target}"
                assert not target.startswith("/"), f"{y.name}:{i} absolute !include: {target}"


def test_single_declared_model_across_tree():
    """Every agent YAML must declare the same model, from the allowed competition list."""
    import yaml

    allowed = {
        "gemma-4-31b-it-qat-w4a16-ct",
        "gemma-4-12b-it-qat-w4a16-ct",
        "gemma-4-e4b-it-qat-w4a16-ct",
        "gemma-4-e2b-it-qat-w4a16-ct",
    }
    models = set()
    for y in [SUBMISSION_DIR / "agent.yaml", *sorted((SUBMISSION_DIR / "sub_agents").glob("*.yaml"))]:
        raw = y.read_text(encoding="utf-8")
        for line in raw.splitlines():
            if line.startswith("model:"):
                models.add(line.split(":", 1)[1].strip().split("/")[-1])
    assert len(models) == 1, f"multiple base models declared: {sorted(models)}"
    assert models <= allowed, f"model not permitted: {models}"


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
