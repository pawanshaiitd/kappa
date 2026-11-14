"""Stage 1: SWE-bench Competition Inference Evaluation Runner.

Evaluates declarative agent submissions against tasks from tasks.jsonl and outputs
submission.parquet with schema [id: string, prediction: string], conforming to
Kaggle and Google SWE-bench harness specifications.

Usage:
    python scripts/inference.py --submission-dir sample_submission --tasks tasks.jsonl --output submission.parquet
    python scripts/inference.py --submission-dir sample_submission --task-id fastapi_15589 --output submission.parquet
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_submission import validate_submission_dir
from scripts.run_gemini_benchmark import GeminiAgentRunner, extract_snapshot_to_dir
from scripts.harness_tools import strip_protected_files_from_patch


def load_submission_eval_config(submission_dir: Path) -> Dict[str, Any]:
    eval_cfg_file = submission_dir / "eval_config.yaml"
    if eval_cfg_file.exists():
        import yaml
        try:
            with open(eval_cfg_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                if isinstance(data, dict) and "evaluation" in data:
                    return data["evaluation"]
        except Exception as e:
            print(f"[Warning] Could not parse eval_config.yaml: {e}")
    return {}


def run_inference(
    submission_dir: Path,
    tasks_file: Path,
    output_parquet: Path,
    snapshots_dir: Path,
    task_id: Optional[str] = None,
    task_idx: Optional[int] = None,
    max_tasks: Optional[int] = None,
    model: str = "gemini-2.5-flash",
    max_turns: int = 15,
):
    print("=" * 70)
    print("  STAGE 1: SWEGEMMA INFERENCE EVALUATION")
    print("=" * 70)
    print(f"Submission Dir: {submission_dir}")
    print(f"Tasks File:     {tasks_file}")
    print(f"Output Parquet: {output_parquet}")

    # 1. Validate submission directory
    val_res = validate_submission_dir(submission_dir)
    if not val_res["valid"]:
        print(f"[Validation Error] Submission is invalid: {val_res.get('error')}")
        sys.exit(1)
    print(f"[Validation] Declared Model: {val_res['declared_model']} | Size: {val_res['total_size_mb']} MB")

    eval_cfg = load_submission_eval_config(submission_dir)
    effective_turns = eval_cfg.get("max_turns", max_turns)

    # 2. Load tasks
    with open(tasks_file, "r", encoding="utf-8") as f:
        all_tasks = [json.loads(line) for line in f if line.strip()]

    target_tasks = []
    if task_id:
        target_tasks = [t for t in all_tasks if t["instance_id"] == task_id]
        if not target_tasks:
            print(f"[Error] Task ID '{task_id}' not found.")
            sys.exit(1)
    elif task_idx is not None:
        if 0 <= task_idx < len(all_tasks):
            target_tasks = [all_tasks[task_idx]]
        else:
            print(f"[Error] Index {task_idx} out of range.")
            sys.exit(1)
    else:
        target_tasks = all_tasks[:max_tasks] if max_tasks else all_tasks

    print(f"Selected {len(target_tasks)} task(s) for evaluation.")

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "dummy"
    results_records: List[Dict[str, str]] = []

    start_total = time.perf_counter()

    for idx, task in enumerate(target_tasks):
        inst_id = task["instance_id"]
        repo = task["repo"]
        snap_file = snapshots_dir / f"{inst_id}.tgz"
        if not snap_file.exists():
            print(f"[Skip] Snapshot for {inst_id} not found at {snap_file}")
            results_records.append({"id": inst_id, "prediction": ""})
            continue

        print(f"\n--- [{idx + 1}/{len(target_tasks)}] Evaluating {inst_id} ({repo}) ---")
        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp(prefix=f"swegemma_infer_{inst_id}_")
        workspace = Path(temp_dir) / "workspace"

        try:
            extract_snapshot_to_dir(snap_file, workspace)

            runner = GeminiAgentRunner(
                task=task,
                workspace_dir=workspace,
                api_key=api_key,
                model=model,
                max_turns=effective_turns,
                submission_dir=submission_dir,
            )

            res = runner.run_agent_loop()
            agent_patch = strip_protected_files_from_patch(res.get("patch", ""))

            print(f"[{inst_id}] Completed in {res['turns_taken']} turns | Patch Size: {len(agent_patch)} bytes")
            results_records.append({"id": inst_id, "prediction": agent_patch})

        except Exception as e:
            print(f"[{inst_id}] Exception during inference: {e}")
            results_records.append({"id": inst_id, "prediction": ""})
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # 3. Write submission.parquet
    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pydict({
        "id": [r["id"] for r in results_records],
        "prediction": [r["prediction"] for r in results_records],
    })
    pq.write_table(table, str(output_parquet))

    elapsed = time.perf_counter() - start_total
    print("\n" + "=" * 70)
    print("  INFERENCE EVALUATION COMPLETED")
    print("=" * 70)
    print(f"Total Tasks:     {len(results_records)}")
    print(f"Non-empty Diffs: {sum(1 for r in results_records if r['prediction'].strip())}")
    print(f"Total Time:      {elapsed:.2f} seconds")
    print(f"Artifact Saved:  {output_parquet} ({output_parquet.stat().st_size} bytes)")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Run Stage 1 Inference for swegemma.")
    parser.add_argument("--submission-dir", type=str, default="sample_submission", help="Path to submission directory")
    parser.add_argument("--tasks", type=str, default="tasks.jsonl", help="Path to tasks.jsonl")
    parser.add_argument("--output", type=str, default="submission.parquet", help="Output path for submission.parquet")
    parser.add_argument("--snapshots-dir", type=str, default="snapshots", help="Path to snapshots directory")
    parser.add_argument("--task-id", type=str, default=None, help="Evaluate specific task ID")
    parser.add_argument("--task-idx", type=int, default=None, help="Evaluate specific task index")
    parser.add_argument("--max-tasks", type=int, default=None, help="Max tasks to evaluate")
    parser.add_argument("--model", type=str, default="gemini-2.5-flash", help="Model alias")
    parser.add_argument("--max-turns", type=int, default=15, help="Max agent turns")
    args = parser.parse_args()

    run_inference(
        submission_dir=REPO_ROOT / args.submission_dir,
        tasks_file=REPO_ROOT / args.tasks,
        output_parquet=REPO_ROOT / args.output,
        snapshots_dir=REPO_ROOT / args.snapshots_dir,
        task_id=args.task_id,
        task_idx=args.task_idx,
        max_tasks=args.max_tasks,
        model=args.model,
        max_turns=args.max_turns,
    )


if __name__ == "__main__":
    main()
