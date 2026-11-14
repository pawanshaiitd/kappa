#!/usr/bin/env python3
"""Run one submission variant over a fixed task set and report a comparable score.

This is the measurement tool behind sample_submission/IMPROVEMENT_PLAN.md. Every experiment
is "change one thing, run the same tasks, compare". It never mutates the real submission:
overrides are applied to a temporary copy.

    # 1. find which tasks this machine can actually score (runs each task's reference patch)
    python scripts/run_experiment.py --validate-eval-set \
        --out-eval-set experiments/eval_sets/all_scorable.json

    # 2. draw a set from that pool in the real repo proportions
    python scripts/run_experiment.py --make-eval-set 30 \
        --from-pool experiments/eval_sets/all_scorable.json --match-distribution \
        --eval-set experiments/eval_sets/set30_matched.json

    # 3. warm the test venvs before spending any model tokens
    python scripts/run_experiment.py --name prep --prebuild-envs \
        --eval-set experiments/eval_sets/set30_matched.json

    # 4. baseline, as the submission currently stands
    python scripts/run_experiment.py --name baseline --eval-set experiments/eval_sets/set30_matched.json

    # one-variable variants
    python scripts/run_experiment.py --name no-subagents --no-subagents       --eval-set experiments/eval_sets/set30_matched.json
    python scripts/run_experiment.py --name think-0      --thinking-budget 0  --eval-set experiments/eval_sets/set30_matched.json
    python scripts/run_experiment.py --name model-12b    --declare-model gemma-4-12b-it-qat-w4a16-ct \
        --backend openai --api-base http://127.0.0.1:8000/v1 --eval-set experiments/eval_sets/set30_matched.json
    python scripts/run_experiment.py --name prompt-short --system-prompt prompts/system_short.md \
        --eval-set experiments/eval_sets/set30_matched.json

    # compare everything run so far
    python scripts/run_experiment.py --compare

Results land in results/experiments/<name>/: summary.json, task_results.jsonl, patches/.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from scripts.submission_harness import (  # noqa: E402
    C_BOLD,
    C_CYAN,
    C_GREEN,
    C_RED,
    C_RESET,
    C_YELLOW,
    Submission,
    classify_failure,
    make_backend,
    run_task,
)
from scripts.run_gemini_benchmark import verify_agent_patch  # noqa: E402

EXPERIMENTS_DIR = REPO_ROOT / "results" / "experiments"


# --------------------------------------------------------------------------------------
# Eval set
# --------------------------------------------------------------------------------------

def load_tasks(tasks_file: Path) -> List[Dict[str, Any]]:
    return [json.loads(l) for l in tasks_file.read_text(encoding="utf-8").splitlines() if l.strip()]


def make_eval_set(tasks_file: Path, snapshots: Path, n: int, seed: int, out: Path,
                  pool: Optional[List[str]] = None, match_distribution: bool = False) -> None:
    """Pick n tasks stratified by repo, deterministically, and freeze the ids to a file.

    Freezing matters: comparing a variant against a baseline scored on different tasks is
    meaningless, and a repo-stratified sample keeps fastapi from dominating the signal.
    """
    tasks = [t for t in load_tasks(tasks_file) if (snapshots / f"{t['instance_id']}.tgz").exists()]
    # Target proportions come from the FULL task file, not from the pool: the pool is whatever this
    # machine can score, and sampling in its proportions would bake its biases into every result.
    target_share = collections.Counter(t["repo"] for t in tasks)
    total = sum(target_share.values())

    if pool is not None:
        allowed = set(pool)
        tasks = [t for t in tasks if t["instance_id"] in allowed]

    by_repo: Dict[str, List[str]] = collections.defaultdict(list)
    for t in tasks:
        by_repo[t["repo"]].append(t["instance_id"])
    rng = random.Random(seed)
    for ids in by_repo.values():
        ids.sort()
        rng.shuffle(ids)

    chosen: List[str] = []
    if match_distribution:
        quota = {r: max(1, round(n * target_share[r] / total)) for r in by_repo}
        for r in sorted(quota, key=lambda r: -quota[r]):
            take = min(quota[r], len(by_repo[r]))
            chosen.extend(by_repo[r][:take])
            by_repo[r] = by_repo[r][take:]
    # Round-robin top-up (also the whole selection when not matching a distribution).
    repos = sorted(by_repo, key=lambda r: -len(by_repo[r]))
    while len(chosen) < n and any(by_repo[r] for r in repos):
        for r in repos:
            if by_repo[r] and len(chosen) < n:
                chosen.append(by_repo[r].pop())
    chosen = sorted(chosen[:n])

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"seed": seed, "count": len(chosen), "instance_ids": chosen}, indent=2),
        encoding="utf-8",
    )
    counts = collections.Counter(
        next(t["repo"] for t in tasks if t["instance_id"] == i) for i in chosen
    )
    print(f"Wrote {out} with {len(chosen)} tasks: {dict(counts)}")


def validate_eval_set(tasks_file: Path, snapshots: Path, ids: List[str], out: Optional[Path]) -> None:
    """Verify each task's REFERENCE patch through Phase 2, with no API calls.

    The gold patch must make the hidden tests pass. Where it does not, this machine cannot
    score that task at all - typically because the venv's dependency versions do not match an
    older base_commit (e.g. starlette's Router signature vs an old fastapi). Such a task would
    report 0 for every variant and silently drag every measurement down, so it has to be
    excluded from the eval set rather than blamed on the agent.
    """
    import contextlib
    import io

    all_tasks = {t["instance_id"]: t for t in load_tasks(tasks_file)}
    usable, broken = [], []
    for n, iid in enumerate(ids, 1):
        task = all_tasks.get(iid)
        snap = snapshots / f"{iid}.tgz"
        if not task or not snap.exists():
            broken.append((iid, "missing task or snapshot"))
            print(f"[{n}/{len(ids)}] {iid}: {C_RED}missing{C_RESET}")
            continue
        gold = task.get("patch") or ""
        if not gold.strip():
            broken.append((iid, "no reference patch in tasks.jsonl"))
            print(f"[{n}/{len(ids)}] {iid}: {C_RED}no gold patch{C_RESET}")
            continue
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                v = verify_agent_patch(task, snap, gold)
            ok = bool(v.get("resolved"))
        except Exception as e:  # noqa: BLE001
            ok, v = False, {"error": str(e)}
        if ok:
            usable.append(iid)
            print(f"[{n}/{len(ids)}] {iid}: {C_GREEN}scorable{C_RESET}")
        else:
            reason = str(v.get("error") or "")
            tail = [l for l in buf.getvalue().splitlines() if l.strip().startswith(("E ", "ERROR"))]
            reason = reason or (tail[-1].strip()[:110] if tail else f"exit {v.get('exit_code')}")
            broken.append((iid, reason))
            print(f"[{n}/{len(ids)}] {iid}: {C_YELLOW}NOT scorable here{C_RESET} - {reason}")

    print(f"\n{'='*72}")
    print(f"  scorable: {C_GREEN}{len(usable)}{C_RESET} / {len(ids)}   "
          f"not scorable: {C_YELLOW}{len(broken)}{C_RESET}")
    print("=" * 72)
    for iid, why in broken:
        print(f"  - {iid}: {why}")
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "validated": True,
                    "count": len(usable),
                    "instance_ids": sorted(usable),
                    "excluded": {i: w for i, w in broken},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nWrote validated eval set to {out} ({len(usable)} tasks)")


def prebuild_envs(tasks_file: Path, snapshots: Path, ids: List[str]) -> None:
    """Create every cached test venv an eval set needs, before any model is called."""
    import tarfile

    from scripts.test_envs import REQUIREMENT_FILES, ensure_env, env_key, resolve_requirements

    all_tasks = {t["instance_id"]: t for t in load_tasks(tasks_file)}
    wanted: Dict[str, Dict[str, Any]] = {}
    for iid in ids:
        task = all_tasks.get(iid)
        snap = snapshots / f"{iid}.tgz"
        if not task or not snap.exists():
            continue
        with tempfile.TemporaryDirectory() as td:
            with tarfile.open(snap, "r:gz") as tar:
                members = [
                    m for m in tar.getmembers()
                    if Path(m.name).name in (*REQUIREMENT_FILES, "pyproject.toml")
                    or Path(m.name).name.startswith("requirements")
                ]
                members = [m for m in members if m.name.count("/") <= 1]
                tar.extractall(path=td, members=members, filter="data")
            reqs = resolve_requirements(Path(td), task["repo"])
        wanted.setdefault(env_key(reqs), {"requirements": reqs, "tasks": []})["tasks"].append(iid)

    print(f"{len(ids)} tasks need {len(wanted)} distinct environments")
    failures = 0
    for key, info in wanted.items():
        res = ensure_env(info["requirements"])
        state = f"{C_GREEN}ok{C_RESET}" if res["ok"] else f"{C_RED}FAILED{C_RESET}"
        print(f"  {key}  {state}  {len(info['tasks'])} task(s)")
        if not res["ok"]:
            failures += 1
            print(f"    {str(res['error'])[:300]}")
            print(f"    affects: {', '.join(info['tasks'])}")
    if failures:
        print(f"\n{C_YELLOW}{failures} environment(s) failed; those tasks will fall back to the "
              f"shared .venv and may be unscorable.{C_RESET}")
    else:
        print(f"\n{C_GREEN}All environments ready.{C_RESET}")


# --------------------------------------------------------------------------------------
# Variant construction
# --------------------------------------------------------------------------------------

def build_variant(src: Path, dest: Path, args: argparse.Namespace) -> List[str]:
    """Copy the submission and apply the requested one-variable overrides."""
    shutil.copytree(src, dest, dirs_exist_ok=True)
    applied: List[str] = []

    agent_file = dest / "agent.yaml"
    agent_raw = agent_file.read_text(encoding="utf-8")

    if args.no_subagents:
        lines, skip = [], False
        for line in agent_raw.splitlines():
            if line.strip().startswith("- agent_tool:"):
                skip = True
                continue
            if skip:
                if line.startswith("      ") or not line.strip():
                    continue
                skip = False
            lines.append(line)
        agent_raw = "\n".join(lines) + "\n"
        applied.append("no_subagents")

    if args.no_graph_tools:
        agent_raw = "\n".join(
            l
            for l in agent_raw.splitlines()
            if l.strip() not in ("- get_code_neighbors", "- search_similar_code", "- get_code_subgraph")
        ) + "\n"
        applied.append("no_graph_tools")

    if args.no_skills:
        lines, skip = [], False
        for line in agent_raw.splitlines():
            if line.strip() == "skills:":
                skip = True
                continue
            if skip:
                if line.strip().startswith("- "):
                    continue
                skip = False
            lines.append(line)
        agent_raw = "\n".join(lines) + "\n"
        shutil.rmtree(dest / "skills", ignore_errors=True)
        applied.append("no_skills")

    if args.system_prompt:
        new_prompt = (REPO_ROOT / args.system_prompt)
        if not new_prompt.exists():
            new_prompt = src / args.system_prompt
        if not new_prompt.exists():
            raise SystemExit(f"system prompt not found: {args.system_prompt}")
        (dest / "prompts" / "system.md").write_text(
            new_prompt.read_text(encoding="utf-8"), encoding="utf-8"
        )
        applied.append(f"system_prompt={args.system_prompt}")

    agent_file.write_text(agent_raw, encoding="utf-8")

    if args.declare_model:
        for y in [agent_file, *sorted((dest / "sub_agents").glob("*.yaml"))]:
            txt = y.read_text(encoding="utf-8")
            y.write_text(
                "\n".join(
                    f"model: {args.declare_model}" if l.startswith("model:") else l
                    for l in txt.splitlines()
                )
                + "\n",
                encoding="utf-8",
            )
        applied.append(f"declare_model={args.declare_model}")

    samp_file = dest / "configs" / "sampling.yaml"
    samp = yaml.safe_load(samp_file.read_text(encoding="utf-8")) or {}
    changed = False
    if args.thinking_budget is not None:
        samp.setdefault("thinking_config", {})["thinking_budget"] = args.thinking_budget
        changed = True
        applied.append(f"thinking_budget={args.thinking_budget}")
    if args.temperature is not None:
        samp["temperature"] = args.temperature
        changed = True
        applied.append(f"temperature={args.temperature}")
    if args.max_output_tokens is not None:
        samp["max_output_tokens"] = args.max_output_tokens
        changed = True
        applied.append(f"max_output_tokens={args.max_output_tokens}")
    if changed:
        samp_file.write_text(yaml.safe_dump(samp, sort_keys=False), encoding="utf-8")
        # sub-agents carry sampling inline, so mirror the change there too
        for y in sorted((dest / "sub_agents").glob("*.yaml")):
            cfg = yaml.safe_load(y.read_text(encoding="utf-8"))
            cfg["generate_content_config"] = samp
            y.write_text(yaml.safe_dump(cfg, sort_keys=False, width=10**6), encoding="utf-8")

    ec_file = dest / "eval_config.yaml"
    ec = yaml.safe_load(ec_file.read_text(encoding="utf-8")) or {"evaluation": {}}
    for key, val in (
        ("max_tool_calls", args.max_tool_calls),
        ("max_time_minutes", args.max_time_minutes),
    ):
        if val is not None:
            ec["evaluation"][key] = val
            applied.append(f"{key}={val}")
    ec_file.write_text(yaml.safe_dump(ec, sort_keys=False), encoding="utf-8")

    return applied


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------

def summarise(name: str, variant: List[str], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    done = [r for r in rows if not r.get("crashed")]
    resolved = [r for r in done if r.get("resolved")]
    buckets = collections.Counter(r.get("failure_bucket", "crashed") for r in rows)
    errors: "collections.Counter[str]" = collections.Counter()
    for r in done:
        errors.update(r.get("tool_errors") or {})

    # The aggregate counters say run_command fails; these say WHICH commands fail, which is the
    # only way to turn a failure rate into a prompt change.
    failing_cmds: "collections.Counter[str]" = collections.Counter()
    for r in done:
        for fc in r.get("failed_calls") or []:
            if fc.get("tool") == "run_command":
                first = str(fc.get("arg", "")).strip().split()
                failing_cmds[" ".join(first[:2]) or "(empty)"] += 1

    def med(key: str) -> Optional[float]:
        vals = [r[key] for r in done if isinstance(r.get(key), (int, float))]
        return round(statistics.median(vals), 1) if vals else None

    return {
        "name": name,
        "variant": variant or ["baseline"],
        "tasks": len(rows),
        "resolved": len(resolved),
        "resolution_rate": round(len(resolved) / len(rows), 4) if rows else 0.0,
        "failure_buckets": dict(buckets),
        "median_turns": med("turns"),
        "median_tool_calls": med("tool_calls"),
        "median_seconds": med("elapsed_seconds"),
        "median_first_edit_turn": med("first_edit_turn"),
        "submitted_explicitly": sum(1 for r in done if r.get("patch_submitted")),
        "empty_patches": sum(1 for r in done if not r.get("patch_size")),
        "tool_errors": dict(errors.most_common(12)),
        "failing_commands": dict(failing_cmds.most_common(12)),
        "test_envs": dict(collections.Counter(
            r.get("test_env", "unknown") for r in done if r.get("resolved") is not None
        )),
        "end_reasons": dict(collections.Counter(r.get("end_reason") for r in done)),
    }


def print_summary(s: Dict[str, Any]) -> None:
    print(f"\n{'='*72}")
    print(f"{C_BOLD}  {s['name']}{C_RESET}   variant: {', '.join(s['variant'])}")
    print("=" * 72)
    rate = s["resolution_rate"]
    col = C_GREEN if rate > 0 else C_RED
    print(f"  resolution rate : {col}{rate:.3f}{C_RESET}  ({s['resolved']}/{s['tasks']})")
    print(f"  failure buckets : {s['failure_buckets']}")
    print(f"  end reasons     : {s['end_reasons']}")
    print(
        f"  medians         : turns={s['median_turns']} calls={s['median_tool_calls']} "
        f"secs={s['median_seconds']} first_edit_turn={s['median_first_edit_turn']}"
    )
    print(f"  empty patches   : {s['empty_patches']}   explicit submits: {s['submitted_explicitly']}")
    if s["tool_errors"]:
        print(f"  tool errors     : {s['tool_errors']}")
    if s.get("failing_commands"):
        print(f"  failing commands: {s['failing_commands']}")


def compare() -> None:
    rows = []
    for d in sorted(EXPERIMENTS_DIR.glob("*/summary.json")):
        rows.append(json.loads(d.read_text(encoding="utf-8")))
    if not rows:
        print("No experiments in results/experiments yet.")
        return
    rows.sort(key=lambda r: -r["resolution_rate"])
    width = max(len(r["name"]) for r in rows) + 2
    print(f"\n{C_BOLD}{'experiment'.ljust(width)}{'rate':>7}{'resolved':>10}{'tasks':>7}"
          f"{'turns':>7}{'secs':>8}  variant{C_RESET}")
    for r in rows:
        print(
            f"{r['name'].ljust(width)}{r['resolution_rate']:>7.3f}{r['resolved']:>10}"
            f"{r['tasks']:>7}{str(r['median_turns']):>7}{str(r['median_seconds']):>8}  "
            f"{', '.join(r['variant'])}"
        )
    print(
        f"\n{C_YELLOW}Only compare rows run on the same eval set. A difference of one or two "
        f"tasks on a 24-task set is noise.{C_RESET}"
    )


# --------------------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--name", help="experiment name (directory under results/experiments)")
    ap.add_argument("--submission-dir", default=str(REPO_ROOT / "sample_submission"))
    ap.add_argument("--tasks", default=str(REPO_ROOT / "tasks.jsonl"))
    ap.add_argument("--snapshots-dir", default=str(REPO_ROOT / "snapshots"))
    ap.add_argument("--eval-set", help="JSON file of frozen instance_ids")
    ap.add_argument("--task-ids", nargs="+", help="explicit ids instead of an eval set")
    ap.add_argument("--limit", type=int, help="only the first N tasks of the eval set")
    ap.add_argument("--make-eval-set", type=int, metavar="N", help="create an eval set and exit")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--from-pool", help="JSON eval set to draw from (e.g. the scorable audit output)")
    ap.add_argument("--match-distribution", action="store_true",
                    help="sample repos in the proportions of the full task file")
    ap.add_argument("--compare", action="store_true", help="print all past experiments and exit")
    ap.add_argument(
        "--validate-eval-set",
        action="store_true",
        help="verify each task's reference patch through Phase 2 (no API calls) and write the "
             "scorable subset to --out-eval-set",
    )
    ap.add_argument("--out-eval-set", help="where --validate-eval-set writes the scorable subset")

    ap.add_argument("--backend", choices=["gemini", "openai", "mock"], default="gemini")
    ap.add_argument("--model", help="model actually called (defaults to the declared one)")
    ap.add_argument("--api-base", default="http://127.0.0.1:8000/v1")

    # one-variable overrides
    ap.add_argument("--no-subagents", action="store_true")
    ap.add_argument("--no-graph-tools", action="store_true")
    ap.add_argument("--no-skills", action="store_true")
    ap.add_argument("--system-prompt", help="path to a replacement prompts/system.md")
    ap.add_argument("--declare-model")
    ap.add_argument("--thinking-budget", type=int)
    ap.add_argument("--temperature", type=float)
    ap.add_argument("--max-output-tokens", type=int)
    ap.add_argument("--max-tool-calls", type=int)
    ap.add_argument("--max-time-minutes", type=float)

    ap.add_argument("--test-env", choices=["auto", "shared"], default="auto",
                    help="Phase 2 dependency handling (see submission_harness --test-env)")
    ap.add_argument("--prebuild-envs", action="store_true",
                    help="build every test venv the eval set needs, then exit. Run this once before "
                         "a sweep: building a venv mid-sweep is slow and a transient index failure "
                         "would otherwise cost that task.")
    ap.add_argument("--quiet", action="store_true", help="suppress per-turn agent output")
    ap.add_argument("--keep-variant", action="store_true", help="keep the temp submission copy")
    args = ap.parse_args()

    if args.compare:
        compare()
        return

    os.environ["SWE_TEST_ENV"] = args.test_env

    tasks_file = Path(args.tasks)
    snapshots = Path(args.snapshots_dir)

    if args.validate_eval_set:
        if args.task_ids:
            ids = args.task_ids
        elif args.eval_set:
            ids = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))["instance_ids"]
        else:
            ids = [
                t["instance_id"]
                for t in load_tasks(tasks_file)
                if (snapshots / f"{t['instance_id']}.tgz").exists()
            ]
        if args.limit:
            ids = ids[: args.limit]
        validate_eval_set(
            tasks_file, snapshots, ids, Path(args.out_eval_set) if args.out_eval_set else None
        )
        return

    if args.make_eval_set:
        out = Path(args.eval_set or REPO_ROOT / "experiments" / "eval_sets" /
                   f"set{args.make_eval_set}.json")
        pool = None
        if args.from_pool:
            pool = json.loads(Path(args.from_pool).read_text(encoding="utf-8"))["instance_ids"]
        make_eval_set(tasks_file, snapshots, args.make_eval_set, args.seed, out,
                      pool=pool, match_distribution=args.match_distribution)
        return

    if not args.name:
        raise SystemExit("--name is required (or use --compare / --make-eval-set)")

    all_tasks = {t["instance_id"]: t for t in load_tasks(tasks_file)}
    if args.task_ids:
        ids = args.task_ids
    elif args.eval_set:
        ids = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))["instance_ids"]
    else:
        raise SystemExit("pass --eval-set or --task-ids")
    if args.limit:
        ids = ids[: args.limit]
    missing = [i for i in ids if i not in all_tasks]
    if missing:
        raise SystemExit(f"unknown instance_ids: {missing}")

    if args.prebuild_envs:
        prebuild_envs(tasks_file, snapshots, ids)
        return

    variant_root = Path(tempfile.mkdtemp(prefix="swegemma_variant_"))
    variant_dir = variant_root / "submission"
    applied = build_variant(Path(args.submission_dir), variant_dir, args)

    sub = Submission(variant_dir)
    problems = sub.validate()
    if problems:
        print(f"{C_RED}Variant does not validate; aborting:{C_RESET}")
        for p in problems:
            print(f"  x {p}")
        if not args.keep_variant:
            shutil.rmtree(variant_root, ignore_errors=True)
        raise SystemExit(2)

    model = args.model or sub.root.model or ""
    if args.backend == "gemini" and not args.model:
        model = "gemini-2.5-flash"
    backend = make_backend(args.backend, model, api_base=args.api_base)

    out_dir = EXPERIMENTS_DIR / args.name
    (out_dir / "patches").mkdir(parents=True, exist_ok=True)
    results_file = out_dir / "task_results.jsonl"
    results_file.write_text("", encoding="utf-8")

    print(f"{C_BOLD}experiment:{C_RESET} {args.name}   "
          f"{C_BOLD}variant:{C_RESET} {', '.join(applied) or 'baseline'}")
    print(f"{C_BOLD}model:{C_RESET} {model} via {args.backend}   "
          f"{C_BOLD}tasks:{C_RESET} {len(ids)}")

    rows: List[Dict[str, Any]] = []
    try:
        for n, iid in enumerate(ids, 1):
            task = all_tasks[iid]
            snap = snapshots / f"{iid}.tgz"
            print(f"\n{C_CYAN}[{n}/{len(ids)}] {iid} ({task['repo']}){C_RESET}")
            if not snap.exists():
                rows.append({"instance_id": iid, "crashed": True, "error": "missing snapshot"})
            else:
                try:
                    r = run_task(sub, task, snap, backend, verify=True, verbose=not args.quiet)
                    patch = r.pop("patch")
                    (out_dir / "patches" / f"{iid}.patch").write_text(patch, encoding="utf-8")
                    r["failure_bucket"] = classify_failure(r)
                    rows.append(r)
                    mark = f"{C_GREEN}RESOLVED{C_RESET}" if r.get("resolved") else (
                        f"{C_RED}{r['failure_bucket']}{C_RESET}")
                    print(f"  -> {mark}  turns={r['turns']} calls={r['tool_calls']} "
                          f"patch={r['patch_size']}B {r['elapsed_seconds']}s")
                except KeyboardInterrupt:
                    raise
                except Exception as e:  # noqa: BLE001 - one bad task must not lose the run
                    print(f"  {C_RED}crashed: {e}{C_RESET}")
                    traceback.print_exc(limit=3)
                    rows.append({"instance_id": iid, "crashed": True, "error": str(e)})
            # Fail fast on a systemic problem (a rejected API key, an unreachable endpoint): if the
            # first few tasks all die the same way, the remaining ones will too, and a long sweep
            # would burn hours to produce a meaningless row of zeros.
            crashed = [r for r in rows if r.get("crashed")]
            if len(rows) >= 3 and len(crashed) == len(rows):
                signatures = {str(r.get("error", ""))[:60] for r in crashed}
                if len(signatures) == 1:
                    print(
                        f"\n{C_RED}Aborting: the first {len(rows)} tasks all failed identically "
                        f"- this is an environment problem, not a result.{C_RESET}\n"
                        f"  {next(iter(signatures))}"
                    )
                    break
            with results_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rows[-1]) + "\n")
    except KeyboardInterrupt:
        print(f"\n{C_YELLOW}Interrupted; summarising what finished.{C_RESET}")
    finally:
        if args.keep_variant:
            print(f"Variant kept at {variant_dir}")
        else:
            shutil.rmtree(variant_root, ignore_errors=True)

    summary = summarise(args.name, applied, rows)
    summary["eval_set"] = args.eval_set or "explicit-ids"
    summary["model"] = model
    summary["backend"] = args.backend
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print_summary(summary)
    print(f"\nArtifacts: {out_dir}")


if __name__ == "__main__":
    main()
