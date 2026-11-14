"""Stage 2: SWE-bench Competition Scoring Engine.

Reads solution (tasks.jsonl) and submission (submission.parquet), executes Phase 2
hermetic verification in Container B sandboxes, and calculates the official Resolution Rate:
    Resolution Rate = (Resolved Tasks) / (Total Tasks in Evaluation Split)

Conforms strictly to Kaggle / Google swegemma evaluation specifications.

Usage:
    python scripts/metric.py --solution tasks.jsonl --submission submission.parquet
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, Any, List
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_gemini_benchmark import verify_agent_patch
from scripts.harness_tools import strip_protected_files_from_patch


def score(
    solution_file: Path,
    submission_parquet: Path,
    snapshots_dir: Path,
    output_report: Path,
) -> float:
    print("=" * 70)
    print("  STAGE 2: SWEGEMMA COMPETITION SCORING & VERIFICATION")
    print("=" * 70)
    print(f"Solution File:      {solution_file}")
    print(f"Submission Parquet: {submission_parquet}")
    print(f"Snapshots Dir:      {snapshots_dir}")

    if not solution_file.exists():
        print(f"[Error] Solution file not found: {solution_file}")
        sys.exit(1)
    if not submission_parquet.exists():
        print(f"[Error] Submission parquet not found: {submission_parquet}")
        sys.exit(1)

    # 1. Read solution tasks
    with open(solution_file, "r", encoding="utf-8") as f:
        tasks_map = {t["instance_id"]: t for t in (json.loads(line) for line in f if line.strip())}

    # 2. Read predictions
    table = pq.read_table(str(submission_parquet))
    pydict = table.to_pydict()
    preds_map = dict(zip(pydict["id"], pydict["prediction"]))

    total_tasks = len(preds_map)
    resolved_count = 0
    detailed_results: List[Dict[str, Any]] = []

    start_time = time.perf_counter()

    for idx, (inst_id, patch) in enumerate(preds_map.items()):
        task = tasks_map.get(inst_id)
        if not task:
            print(f"[{inst_id}] Warning: Task ID not in solution set.")
            continue

        clean_patch = strip_protected_files_from_patch(patch)
        snap_file = snapshots_dir / f"{inst_id}.tgz"

        print(f"\n--- [{idx + 1}/{total_tasks}] Verifying Task: {inst_id} ({task.get('repo')}) ---")
        print(f"Submitted Patch Size: {len(clean_patch)} bytes")

        if not clean_patch.strip():
            print(f"[{inst_id}] Result: UNRESOLVED (No patch provided)")
            detailed_results.append({
                "instance_id": inst_id,
                "resolved": False,
                "reason": "Empty patch",
            })
            continue

        if not snap_file.exists():
            print(f"[{inst_id}] Result: UNRESOLVED (Snapshot archive missing)")
            detailed_results.append({
                "instance_id": inst_id,
                "resolved": False,
                "reason": "Missing snapshot",
            })
            continue

        verif_res = verify_agent_patch(task, snap_file, clean_patch)
        is_resolved = verif_res.get("resolved", False)

        if is_resolved:
            resolved_count += 1
            print(f"[{inst_id}] Result: RESOLVED (PASS)")
        else:
            print(f"[{inst_id}] Result: UNRESOLVED (Test Failure or Exit Code != 0)")

        detailed_results.append({
            "instance_id": inst_id,
            "resolved": is_resolved,
            "exit_code": verif_res.get("exit_code"),
            "stdout": verif_res.get("stdout", "")[:500],
        })

    resolution_rate = resolved_count / total_tasks if total_tasks > 0 else 0.0
    elapsed = time.perf_counter() - start_time

    output_report.parent.mkdir(parents=True, exist_ok=True)
    summary_data = {
        "total_tasks": total_tasks,
        "resolved_tasks": resolved_count,
        "resolution_rate": resolution_rate,
        "elapsed_seconds": elapsed,
        "task_results": detailed_results,
    }
    output_report.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print("  COMPETITION EVALUATION FINAL SCORECARD")
    print("=" * 70)
    print(f"Total Evaluated Tasks: {total_tasks}")
    print(f"Resolved Tasks:        {resolved_count}")
    print(f"Resolution Rate:       {resolution_rate * 100:.2f}%")
    print(f"Elapsed Time:          {elapsed:.2f} seconds")
    print(f"Report Saved:          {output_report}")
    print("=" * 70)

    return resolution_rate


def main():
    parser = argparse.ArgumentParser(description="Run Stage 2 Metric Scoring for swegemma.")
    parser.add_argument("--solution", type=str, default="tasks.jsonl", help="Path to solution tasks.jsonl")
    parser.add_argument("--submission", type=str, default="submission.parquet", help="Path to submission.parquet")
    parser.add_argument("--snapshots-dir", type=str, default="snapshots", help="Path to snapshots directory")
    parser.add_argument("--output", type=str, default="results/metric_summary.json", help="Summary output JSON")
    args = parser.parse_args()

    score(
        solution_file=REPO_ROOT / args.solution,
        submission_parquet=REPO_ROOT / args.submission,
        snapshots_dir=REPO_ROOT / args.snapshots_dir,
        output_report=REPO_ROOT / args.output,
    )


if __name__ == "__main__":
    main()
