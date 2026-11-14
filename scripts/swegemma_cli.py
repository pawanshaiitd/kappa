#!/usr/bin/env python3
"""swegemma CLI - Local Evaluation Command Runner for Kaggle SWE-bench.

Matches Section 9 of HARNESS_README.md:
    swegemma eval \
      --tasks tasks.jsonl \
      --snapshots-dir snapshots \
      --submission-dir sample_submission \
      --results-dir results/competition_run \
      --sandbox subprocess \
      --task-id fastapi_15589
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_submission import validate_submission_dir
from scripts.run_gemini_benchmark import GeminiAgentRunner, extract_snapshot_to_dir, verify_agent_patch
from scripts.harness_tools import strip_protected_files_from_patch


def run_eval(
    tasks_file: Path,
    snapshots_dir: Path,
    submission_dir: Path,
    results_dir: Path,
    sandbox: str = "subprocess",
    task_id: Optional[str] = None,
    task_ids: Optional[List[str]] = None,
    max_tool_calls: Optional[int] = None,
    max_time_minutes: Optional[float] = None,
    timeout_seconds: int = 300,
    model: str = "gemini-2.5-flash",
    max_turns: int = 15,
):
    print("=" * 70)
    print("  SWEGEMMA CLI: BENCHMARK EVALUATOR")
    print("=" * 70)
    print(f"Tasks:          {tasks_file}")
    print(f"Snapshots:      {snapshots_dir}")
    print(f"Submission:     {submission_dir}")
    print(f"Results Dir:    {results_dir}")
    print(f"Sandbox:        {sandbox}")
    print(f"Model:          {model}")
    print("=" * 70)

    # 1. Validate submission
    val = validate_submission_dir(submission_dir)
    if not val["valid"]:
        print(f"[Error] Invalid submission package: {val.get('error')}")
        sys.exit(1)
    print(f"[Validation Pass] Model: {val['declared_model']} | Size: {val['total_size_mb']} MB")

    # 2. Setup results directories (HARNESS_README Section 9.2)
    patches_dir = results_dir / "patches"
    test_outputs_dir = results_dir / "test_outputs"
    logs_dir = results_dir / "logs"
    for d in (patches_dir, test_outputs_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 3. Filter tasks
    with open(tasks_file, "r", encoding="utf-8") as f:
        all_tasks = [json.loads(line) for line in f if line.strip()]

    target_tasks = []
    if task_id:
        target_tasks = [t for t in all_tasks if t["instance_id"] == task_id]
    elif task_ids:
        target_tasks = [t for t in all_tasks if t["instance_id"] in task_ids]
    else:
        target_tasks = all_tasks

    if not target_tasks:
        print("[Error] No matching tasks found.")
        sys.exit(1)

    print(f"Executing evaluation on {len(target_tasks)} task(s)...")

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "dummy"
    task_results = []
    resolved_count = 0
    start_all = time.perf_counter()

    for idx, task in enumerate(target_tasks):
        inst_id = task["instance_id"]
        repo = task["repo"]
        snap_file = snapshots_dir / f"{inst_id}.tgz"

        print(f"\n>>> [{idx + 1}/{len(target_tasks)}] Task: {inst_id} ({repo}) <<<")

        if not snap_file.exists():
            print(f"[Skip] Snapshot missing: {snap_file}")
            continue

        temp_dir = tempfile.mkdtemp(prefix=f"swegemma_eval_{inst_id}_")
        workspace = Path(temp_dir) / "workspace"

        try:
            # Phase 1: Agent Sandbox
            extract_snapshot_to_dir(snap_file, workspace)

            runner = GeminiAgentRunner(
                task=task,
                workspace_dir=workspace,
                api_key=api_key,
                model=model,
                max_turns=max_turns,
                submission_dir=submission_dir,
            )

            res = runner.run_agent_loop()
            agent_patch = strip_protected_files_from_patch(res.get("patch", ""))

            # Save patch
            patch_file = patches_dir / f"{inst_id}.patch"
            patch_file.write_text(agent_patch, encoding="utf-8")

            # Phase 2: Verification Sandbox
            verif_res = verify_agent_patch(task, snap_file, agent_patch)
            is_resolved = verif_res.get("resolved", False)
            if is_resolved:
                resolved_count += 1

            # Save test output
            (test_outputs_dir / f"{inst_id}.log").write_text(verif_res.get("stdout", "") + "\n" + verif_res.get("stderr", ""), encoding="utf-8")

            task_entry = {
                "instance_id": inst_id,
                "repo": repo,
                "resolved": is_resolved,
                "turns_taken": res["turns_taken"],
                "tool_calls_count": res["tool_calls_count"],
                "patch_size": len(agent_patch),
                "exit_code": verif_res.get("exit_code"),
            }
            task_results.append(task_entry)

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    elapsed = time.perf_counter() - start_all
    resolution_rate = resolved_count / len(task_results) if task_results else 0.0

    # Summary artifacts
    summary_data = {
        "total_tasks": len(task_results),
        "resolved": resolved_count,
        "resolution_rate": resolution_rate,
        "elapsed_seconds": elapsed,
    }
    (results_dir / "summary.json").write_text(json.dumps(summary_data, indent=2), encoding="utf-8")
    with open(results_dir / "task_results.jsonl", "w", encoding="utf-8") as f:
        for r in task_results:
            f.write(json.dumps(r) + "\n")

    print("\n" + "=" * 70)
    print("  SWEGEMMA EVALUATION COMPLETE")
    print("=" * 70)
    print(f"Total Tasks:     {len(task_results)}")
    print(f"Resolved Tasks:  {resolved_count}")
    print(f"Resolution Rate: {resolution_rate * 100:.2f}%")
    print(f"Summary JSON:    {results_dir / 'summary.json'}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(prog="swegemma", description="SWE-bench swegemma harness CLI.")
    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to execute")

    eval_parser = subparsers.add_parser("eval", help="Run local evaluation on benchmark tasks")
    eval_parser.add_argument("--tasks", type=str, default="tasks.jsonl", help="Path to tasks.jsonl")
    eval_parser.add_argument("--snapshots-dir", type=str, default="snapshots", help="Path to snapshots directory")
    eval_parser.add_argument("--submission-dir", type=str, default="sample_submission", help="Path to submission directory")
    eval_parser.add_argument("--results-dir", type=str, default="results/competition_run", help="Path to output directory")
    eval_parser.add_argument("--sandbox", choices=["docker", "subprocess"], default="subprocess", help="Sandbox backend")
    eval_parser.add_argument("--task-id", type=str, default=None, help="Specific task ID to run")
    eval_parser.add_argument("--task-ids", nargs="+", default=None, help="List of task IDs")
    eval_parser.add_argument("--max-tool-calls", type=int, default=50, help="Max tool calls")
    eval_parser.add_argument("--max-time-minutes", type=float, default=30.0, help="Max time minutes")
    eval_parser.add_argument("--timeout-seconds", type=int, default=300, help="Timeout seconds")
    eval_parser.add_argument("--concurrency", type=int, default=1, help="Concurrency")
    eval_parser.add_argument("--display", type=str, default="auto", help="Display mode")
    eval_parser.add_argument("--model", type=str, default="gemini-2.5-flash", help="Model alias")
    eval_parser.add_argument("--max-turns", type=int, default=15, help="Max agent turns")

    args = parser.parse_args()

    if args.subcommand == "eval":
        run_eval(
            tasks_file=REPO_ROOT / args.tasks,
            snapshots_dir=REPO_ROOT / args.snapshots_dir,
            submission_dir=REPO_ROOT / args.submission_dir,
            results_dir=REPO_ROOT / args.results_dir,
            sandbox=args.sandbox,
            task_id=args.task_id,
            task_ids=args.task_ids,
            max_tool_calls=args.max_tool_calls,
            max_time_minutes=args.max_time_minutes,
            timeout_seconds=args.timeout_seconds,
            model=args.model,
            max_turns=args.max_turns,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
