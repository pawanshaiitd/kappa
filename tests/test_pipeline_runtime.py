"""Runtime tests for the local harness: pipeline execution, session state, sandbox isolation,
context accounting and the simulated clock. No network and no model: a scripted backend drives
the real shipped submission against a small throwaway repository.
"""

import argparse
import json
import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

from scripts.submission_harness import (
    AgentSpec,
    MockBackend,
    RootRunner,
    Sandbox,
    StateKeyError,
    Submission,
    build_initial_prompt,
    classify_failure,
    render_instruction,
    run_autonomous,
)

BUDGET = {"timeout_seconds": 30, "max_tool_calls": 22, "max_time_minutes": 3, "max_turns": 26}
TASK = {
    "instance_id": "demo_1",
    "repo": "demo/pkg",
    "problem_statement": "double() returns x + x + 1 but must return twice its argument.",
    "hints_text": "",
}


class RecordingBackend(MockBackend):
    """Scripted backend that also keeps what each request contained."""

    def __init__(self, script):
        super().__init__(script)
        self.requests = []

    def generate(self, system, messages, tools, gen_cfg):
        self.requests.append({"system": system, "messages": messages, "tools": sorted(tools)})
        return super().generate(system, messages, tools, gen_cfg)


def call(name, **args):
    return {"text": "", "tool_calls": [{"name": name, "args": args}]}


def say(text):
    return {"text": text, "tool_calls": []}


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "pkg").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "pkg" / "__init__.py").write_text("from .core import double\n", encoding="utf-8")
    (ws / "pkg" / "core.py").write_text(
        "def double(x):\n    # doubles the value\n    return x + x + 1\n", encoding="utf-8")
    (ws / "tests" / "test_core.py").write_text(
        "from pkg import double\n\n\ndef test_double():\n    assert double(2) == 4\n",
        encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "config", "user.name", "t"],
                ["git", "config", "user.email", "t@t"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "baseline"]):
        subprocess.run(cmd, cwd=str(ws), check=True, capture_output=True)
    return ws


def make_sandbox(ws, **kw):
    budget = dict(BUDGET)
    budget.update(kw.pop("budget", {}))
    return Sandbox(ws, TASK, budget, agent_env="host", **kw)


LOCATION = ("FILE: pkg/core.py\nLINES: 1-3\nSYMBOL: double\nCAUSE: adds one too many\n"
            "FIX: return x + x\nTEST: tests/test_core.py")
PIPELINE_SCRIPT = [
    call("run_command", command='grep -rnE "def double" --include=*.py pkg | head -15'),
    say("DONE"),
    say(LOCATION),
    call("read_file", filepath="pkg/core.py", start_line=1, end_line=3),
    call("edit_file", filepath="pkg/core.py",
         old_string="    return x + x + 1", new_string="    return x + x"),
    say("CHANGED: pkg/core.py double\nWHY: off by one\nRISK: NONE"),
    call("run_command", command="git diff | head -80"),
    call("submit_patch"),
]


# --------------------------------------------------------------------------------------
# Session-state templating
# --------------------------------------------------------------------------------------

def test_render_instruction_required_optional_and_untouched():
    state = {"problem_description": "boom"}
    assert render_instruction("P: {problem_description}", state) == "P: boom"
    assert render_instruction("H: [{hints?}]", state) == "H: []"
    with pytest.raises(StateKeyError):
        render_instruction("L: {location}", state)
    # Not identifiers, so ADK leaves them alone.
    assert render_instruction('{"a": 1} and {} and {a b}', state) == '{"a": 1} and {} and {a b}'


def test_validate_flags_unsafe_state_keys(tmp_path):
    (tmp_path / "agent.yaml").write_text(
        "name: root\nagent_class: SequentialAgent\nsub_agents:\n"
        "  - config_path: a.yaml\n  - config_path: b.yaml\n", encoding="utf-8")
    (tmp_path / "a.yaml").write_text(
        "name: a\nmodel: gemma-4-12b-it-qat-w4a16-ct\noutput_key: found\n"
        "instruction: 'find it {hints}'\ntools: [run_command]\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text(
        "name: b\nmodel: gemma-4-12b-it-qat-w4a16-ct\n"
        "instruction: 'use {found} and {typo_key}'\ntools: [edit_file]\n", encoding="utf-8")
    problems = "\n".join(Submission(tmp_path).validate())
    assert "{hints}" in problems          # unset when a task has no hints
    assert "{found}" in problems          # unset if agent a never gives a final reply
    assert "{typo_key}" in problems       # not a state key at all


# --------------------------------------------------------------------------------------
# The shipped pipeline, end to end
# --------------------------------------------------------------------------------------

def test_pipeline_hands_off_through_state_with_isolated_context(workspace):
    sub = Submission(REPO_ROOT / "sample_submission")
    sb = make_sandbox(workspace)
    backend = RecordingBackend(PIPELINE_SCRIPT)
    root = RootRunner(sub.root, sb, backend, verbose=False)
    root.add_user(build_initial_prompt(TASK, sb, sub.root))
    result = run_autonomous(root, sb)

    assert result["end_reason"] == "submit_patch"
    assert "-    return x + x + 1" in result["patch"] and "+    return x + x" in result["patch"]
    assert result["turns"] == 8 and result["tool_calls"] == 4  # submit_patch is free
    assert result["first_edit_turn"] == 5
    assert set(result["phase_stats"]) == {"locator", "reporter", "fixer", "verifier"}
    assert sb.state["location"] == LOCATION
    assert sb.state["fix_summary"].startswith("CHANGED: pkg/core.py")

    locator_req, reporter_req = backend.requests[0], backend.requests[2]
    fixer_req, verifier_req = backend.requests[3], backend.requests[6]
    # The reporter has no tools and sees the locator's tool results.
    assert not reporter_req.get("tools")
    assert "def double" in json.dumps(reporter_req["messages"])
    # Locator sees the harness prompt; later steps get the problem and report via their instruction.
    assert "Workspace Layout" in locator_req["messages"][0]["text"]
    assert TASK["problem_statement"] in fixer_req["system"]
    assert "FILE: pkg/core.py" in fixer_req["system"]
    assert "CHANGED: pkg/core.py" in verifier_req["system"]
    # include_contents: none - the locator's tool output never reaches the fixer or verifier.
    for req in (fixer_req, verifier_req):
        dumped = json.dumps(req["messages"])
        assert "Workspace Layout" not in dumped
        assert "def double" not in dumped
        assert len(req["messages"]) == 1 and req["messages"][0]["role"] == "user"
    # Within a step, the agent keeps its own tool history.
    assert [m["role"] for m in backend.requests[4]["messages"]] == ["user", "assistant", "tool"]
    assert "submit_patch" in verifier_req["tools"] and "submit_patch" not in fixer_req["tools"]
    assert "edit_file" not in locator_req["tools"]


def test_undeclared_tool_is_rejected_per_agent(workspace):
    """The locator has no edit_file: calling it returns an error and changes nothing."""
    sub = Submission(REPO_ROOT / "sample_submission")
    sb = make_sandbox(workspace)
    script = [call("edit_file", filepath="pkg/core.py", old_string="x + x + 1", new_string="0"),
              say(LOCATION), say("CHANGED: NOTHING\nWHY: -\nRISK: -"), call("submit_patch")]
    root = RootRunner(sub.root, sb, RecordingBackend(script), verbose=False)
    root.add_user("task")
    result = run_autonomous(root, sb)
    assert result["tool_errors"].get("edit_file:UnknownTool") == 1
    assert result["patch"] == "" and result["edits_landed"] == 0


def test_lean_submission_runs_and_recovers_unsubmitted_patch(workspace):
    """No submit_patch: the working tree is still harvested, after the nudge limit."""
    sub = Submission(REPO_ROOT / "sample_submission_lean")
    sb = make_sandbox(workspace)
    script = [call("edit_file", filepath="pkg/core.py",
                   old_string="    return x + x + 1", new_string="    return x + x")]
    root = RootRunner(sub.root, sb, RecordingBackend(script), verbose=False)
    root.add_user("task")
    result = run_autonomous(root, sb)
    assert result["end_reason"] == "nudge_limit" and not result["patch_submitted"]
    assert "+    return x + x" in result["patch"]


# --------------------------------------------------------------------------------------
# Sandbox fidelity
# --------------------------------------------------------------------------------------

def test_run_command_has_private_tmp_and_importable_repo(workspace):
    sb = make_sandbox(workspace)
    marker = "swegemma_isolation_probe.txt"
    res = sb.call("run_command", {"command": f"echo hi > /tmp/{marker} && cat /tmp/{marker}"}, {})
    assert res["status"] == "ok" and res["stdout"].strip() == "hi"
    assert (sb.tmp / marker).exists() and not Path("/tmp", marker).exists()
    res = sb.call("run_command", {"command": "cd /tmp && python3 -c 'import pkg; print(pkg.double(1))'"}, {})
    assert res["status"] == "ok", res
    # /workspace is rewritten, and a path that merely contains "tmp" is left alone.
    res = sb.call("run_command", {"command": "ls /workspace/pkg | head -5"}, {})
    assert "core.py" in res["stdout"]
    assert sb._rewrite_paths("cat build/tmp/x a/tmp") == "cat build/tmp/x a/tmp"


def test_baseline_commit_excludes_build_artifacts(tmp_path):
    from scripts.run_gemini_benchmark import extract_snapshot_to_dir

    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    tgz = tmp_path / "snap.tgz"
    with tarfile.open(tgz, "w:gz") as tar:
        tar.add(src / "pkg", arcname="pkg")
    ws = tmp_path / "workspace"
    extract_snapshot_to_dir(tgz, ws)
    sb = Sandbox(ws, TASK, dict(BUDGET), agent_env="host")
    (ws / "pkg" / "__pycache__").mkdir()
    (ws / "pkg" / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\0" * 2000)
    (ws / "pkg.egg-info").mkdir()
    (ws / "pkg.egg-info" / "PKG-INFO").write_text("x", encoding="utf-8")
    (ws / ".pytest_cache").mkdir()
    (ws / ".pytest_cache" / "v").write_text("x", encoding="utf-8")
    assert sb.live_diff() == ""
    (ws / "pkg" / "new_module.py").write_text("X = 1\n", encoding="utf-8")
    assert "new_module.py" in sb.live_diff()


# --------------------------------------------------------------------------------------
# Budgets, clock and context
# --------------------------------------------------------------------------------------

def single_agent(tmp_path, **cfg):
    base = {"name": "solo", "model": "gemma-4-12b-it-qat-w4a16-ct", "instruction": "fix it",
            "tools": ["run_command", "read_file", "submit_patch"]}
    base.update(cfg)
    return AgentSpec(base, tmp_path, tmp_path / "agent.yaml")


def test_max_turns_counts_every_model_request(workspace, tmp_path):
    sb = make_sandbox(workspace, budget={"max_turns": 5})
    script = [call("run_command", command="true")] * 50
    root = RootRunner(single_agent(tmp_path), sb, RecordingBackend(script), verbose=False)
    root.add_user("task")
    result = run_autonomous(root, sb)
    assert result["end_reason"] == "max_turns" and result["turns"] == 5


def test_simulated_clock_makes_the_time_budget_bind(workspace, tmp_path):
    """At API speed this loop would never hit a 30 s cap; at Gemma speed it does."""
    sim = {"prefill_tps": 100.0, "decode_tps": 5.0}
    sb = make_sandbox(workspace, sim=sim, budget={"max_time_minutes": 0.5, "max_turns": 500,
                                                  "max_tool_calls": 500})
    script = [{"text": "thinking " * 20, "tool_calls": [{"name": "run_command",
                                                          "args": {"command": "true"}}]}] * 200
    root = RootRunner(single_agent(tmp_path), sb, RecordingBackend(script), verbose=False)
    root.add_user("task " * 50)
    result = run_autonomous(root, sb)
    assert result["end_reason"] == "time_budget_exhausted"
    assert result["simulated_clock"] and result["elapsed_seconds"] >= 30
    assert result["turns"] < 20 and result["wall_seconds"] < 20


def test_context_overflow_is_detected(workspace, tmp_path):
    sb = make_sandbox(workspace)
    spec = single_agent(tmp_path, generate_content_config={"max_output_tokens": 32000})
    root = RootRunner(spec, sb, RecordingBackend([call("run_command", command="true")]), verbose=False)
    root.add_user("x" * 6000)
    result = run_autonomous(root, sb)
    assert result["end_reason"] == "context_overflow" and result["turns"] == 0


def test_history_is_compacted_past_the_token_threshold(workspace, tmp_path):
    (workspace / "big.txt").write_text(("a fairly long line of source text, number\n" * 20 + "\n") * 40, encoding="utf-8")
    sb = make_sandbox(workspace)
    spec = single_agent(tmp_path, generate_content_config={"max_output_tokens": 1024})
    script = [call("read_file", filepath="big.txt", start_line=1, end_line=150)] * 12 + [call("submit_patch")]
    root = RootRunner(spec, sb, RecordingBackend(script), verbose=False)
    root.add_user("task statement")
    result = run_autonomous(root, sb)
    assert result["end_reason"] == "submit_patch"
    assert result["compactions"] >= 1
    assert result["prompt_tokens_max"] < 20000
    assert "compacted" in root.events[0]["text"]


def test_make_sim_presets_and_overrides():
    from scripts.submission_harness import SIM_PRESETS, make_sim

    assert make_sim(argparse.Namespace(sim=None, sim_prefill_tps=None, sim_decode_tps=None)) is None
    sim = make_sim(argparse.Namespace(sim="gemma12b", sim_prefill_tps=None, sim_decode_tps=33.0))
    assert sim["decode_tps"] == 33.0 and sim["prefill_tps"] == SIM_PRESETS["gemma12b"]["prefill_tps"]


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------

def test_classify_failure_separates_broken_patches_from_wrong_fixes():
    base = {"patch_size": 500, "edits_landed": 1}
    assert classify_failure({**base, "resolved": True}) == "resolved"
    assert classify_failure({"patch_size": 0, "edits_landed": 0}) == "no_edit_attempted"
    assert classify_failure({**base, "pytest_exit_code": 2,
                             "verify_stdout": "E   NameError: name 'Literal' is not defined\n"
                                              "1 error during collection"}) == "patch_breaks_import"
    assert classify_failure({**base, "pytest_exit_code": 2,
                             "verify_stdout": "E   ModuleNotFoundError: No module named 'typer'"}
                            ) == "test_env_broken"
    assert classify_failure({**base, "pytest_exit_code": 1,
                             "verify_stdout": "E   assert 422 == 200"}) == "tests_failed"
    assert classify_failure({"patch_size": 29_000_000, "edits_landed": 1}) == "patch_polluted"


def test_summary_projects_notebook_time():
    from scripts.run_experiment import summarise

    rows = [{"instance_id": f"t{i}", "resolved": i % 2 == 0, "failure_bucket": "resolved",
             "turns": 20, "tool_calls": 15, "elapsed_seconds": 120.0, "simulated_clock": True,
             "prompt_tokens_max": 6000, "output_tokens_total": 2500, "compactions": 0,
             "phase_stats": {"locator": {"llm_calls": 6, "tool_calls": 5}}} for i in range(4)]
    s = summarise("x", [], rows, setup_seconds=60.0)
    assert s["projection"]["simulated_clock"] is True
    # 300 tasks x (120 s + 60 s setup) = 15 h, plus 0.25 h start-up.
    assert s["projection"]["notebook_hours"]["300"] == 15.2
    assert s["phase_medians"]["locator"]["llm_calls"] == 6
    assert s["max_prompt_tokens"] == 6000


def test_variant_can_drop_a_prompt_section(tmp_path):
    from scripts.run_experiment import build_variant

    args = argparse.Namespace(
        no_subagents=False, no_graph_tools=False, no_skills=False, system_prompt=None,
        declare_model="gemma-4-e4b-it-qat-w4a16-ct", thinking_budget=0, temperature=None,
        max_output_tokens=None, max_tool_calls=None, max_time_minutes=None, max_turns=None,
        drop_section=["Repo notes"], include_thoughts=None)
    dest = tmp_path / "variant"
    applied = build_variant(REPO_ROOT / "sample_submission", dest, args)
    locator = (dest / "prompts" / "locator.md").read_text(encoding="utf-8")
    assert "## Repo notes" not in locator and "## Replies" in locator
    sub = Submission(dest)
    assert sub.validate() == []
    assert {a.model for a in sub.root.llm_agents()} == {"gemma-4-e4b-it-qat-w4a16-ct"}
    assert all(a.gen_cfg["thinking_config"]["thinking_budget"] == 0 for a in sub.root.llm_agents())
    assert "drop_section=Repo notes" in applied
