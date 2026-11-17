# Runbook — iteration 3, on the server

Every command here runs on the server (the Gemini key and the Gemma endpoint are only reachable
there). `$PY` is the interpreter, normally `.venv/bin/python`.

What is being tested:

| Directory | What it is |
|---|---|
| `sample_submission/` | **pipeline**: `SequentialAgent` locator → fixer → verifier, 12B |
| `sample_submission_lean/` | **lean**: one agent, 4 tools, same budgets — the fallback |
| `archive/submission_v2/` | the submission that timed out, kept as the baseline |

The answer you need before zipping is two numbers per variant: the **resolution rate** and the
**projected notebook hours**. Both are printed at the end of every run.

---

## 0. Setup and sanity (no model calls)

```bash
export GOOGLE_API_KEY="..."
```

```bash
PYTHONPATH=. $PY -m pytest tests/ -q
```

```bash
$PY scripts/validate_submission.py sample_submission sample_submission_lean
```

Both must print `[PASS]`. The validator now fails any submission whose per-task cap could run past
the 18 h notebook limit.

```bash
$PY scripts/submission_harness.py --dry-run --task-id fastapi_15589
```

Prints the compiled tree and, per agent, how many tokens are sent on every request.

## 1. Rebuild the eval sets (no model calls, ~1 h)

The patch-extraction and environment fixes change which tasks this machine can score, and
`fastapi_15588` / `fastapi_15661` in the old set were not scorable at all.

```bash
$PY scripts/run_experiment.py --validate-eval-set --out-eval-set experiments/eval_sets/all_scorable.json
```

```bash
$PY scripts/run_experiment.py --make-eval-set 30 --from-pool experiments/eval_sets/all_scorable.json --match-distribution --eval-set experiments/eval_sets/set30_a.json
```

```bash
$PY scripts/run_experiment.py --make-eval-set 30 --seed 11 --from-pool experiments/eval_sets/all_scorable.json --exclude-set experiments/eval_sets/set30_a.json --match-distribution --eval-set experiments/eval_sets/set30_b.json
```

`set30_a` is for tuning, `set30_b` is touched only to confirm the winner.

```bash
$PY scripts/run_experiment.py --name prep --prebuild-envs --eval-set experiments/eval_sets/set30_a.json
```

```bash
$PY scripts/run_experiment.py --name prep --prebuild-envs --eval-set experiments/eval_sets/set30_b.json
```

Both must end with `All environments ready.` Build environments before using `--workers`.

## 2. The experiments

`--workers 4` runs four tasks at once against Gemini. `--resume` continues an interrupted run.
`--sim gemma12b` replaces the wall clock with a projected Gemma clock, so the 3-minute cap binds the
way it will on 4×L4. The preset speeds (1000 prompt tok/s, 20 output tok/s) are **assumptions**;
replace them with measured values from step 4 as soon as the endpoint is up.

**E1 — old submission on the fixed rig** (new baseline; shows how many of the 300 failed commands
were the rig's fault):

```bash
$PY -u scripts/run_experiment.py --name e1-v2-baseline --submission-dir archive/submission_v2 --eval-set experiments/eval_sets/set30_a.json --workers 4 --quiet
```

**E2 — both new variants, no clock simulation** (quality only):

```bash
$PY -u scripts/run_experiment.py --name e2-pipeline --eval-set experiments/eval_sets/set30_a.json --max-time-minutes 30 --workers 4 --quiet
```

```bash
$PY -u scripts/run_experiment.py --name e2-lean --submission-dir sample_submission_lean --eval-set experiments/eval_sets/set30_a.json --max-time-minutes 30 --workers 4 --quiet
```

**E3 — both variants exactly as shipped, on the simulated Gemma clock** (does it fit 18 h):

```bash
$PY -u scripts/run_experiment.py --name e3-pipeline-sim --eval-set experiments/eval_sets/set30_a.json --sim gemma12b --workers 4 --quiet
```

```bash
$PY -u scripts/run_experiment.py --name e3-lean-sim --submission-dir sample_submission_lean --eval-set experiments/eval_sets/set30_a.json --sim gemma12b --workers 4 --quiet
```

**E4 — one variable at a time, on the better variant** (add `--submission-dir sample_submission_lean`
if lean won):

```bash
$PY -u scripts/run_experiment.py --name e4-think0 --thinking-budget 0 --eval-set experiments/eval_sets/set30_a.json --sim gemma12b --workers 4 --quiet
```

```bash
$PY -u scripts/run_experiment.py --name e4-think1024 --thinking-budget 1024 --eval-set experiments/eval_sets/set30_a.json --sim gemma12b --workers 4 --quiet
```

```bash
$PY -u scripts/run_experiment.py --name e4-no-cards --drop-section "Repo notes" --eval-set experiments/eval_sets/set30_a.json --sim gemma12b --workers 4 --quiet
```

**Confirm the winner on the untouched set:**

```bash
$PY -u scripts/run_experiment.py --name e5-confirm --eval-set experiments/eval_sets/set30_b.json --sim gemma12b --workers 4 --quiet
```

## 3. Reading a result

```bash
$PY scripts/run_experiment.py --compare
```

```bash
$PY scripts/run_experiment.py --analyze e3-pipeline-sim
```

`--analyze` prints the failure taxonomy, how sessions ended, the import errors found in Phase 2, the
error rate per tool, the commands that fail most, repeated identical calls, and the slowest tasks.
Per-task traces (every model request and tool call with token counts) are in
`results/experiments/<name>/traces/`.

| Summary line | Accept when |
|---|---|
| `resolution rate` | E2 ≥ E1; ship the variant with the higher E3 rate |
| `failure buckets: patch_breaks_import` | ≤ 2 of 30 (it was 9) |
| `context: max prompt` | under 12 000 tokens, `compactions=0` |
| `notebook (18 h): 300 tasks` | ≤ 12 h (printed red above 15 h) |
| `end reasons: time_budget_exhausted` | a minority; if most tasks end this way, lower `thinking_budget` before raising the cap |
| `failing commands` | under 60 in total; anything frequent becomes a prompt fix |

At 30 tasks a difference of one or two tasks is noise. Judge by the buckets.

## 4. When the Gemma endpoint is up

The default endpoint is `http://10.120.12.55:8000/v1`. Override it with `--api-base` or once with:

```bash
export SWE_API_BASE="http://HOST:PORT/v1"
```

Check it and measure its speed:

```bash
$PY scripts/submission_harness.py --probe-endpoint
```

This lists the served models, makes one tool-call round trip, and prints measured `prefill_tps` and
`decode_tps`. `tool_call_parsed: false` means vLLM is not running with
`--enable-auto-tool-choice --tool-call-parser gemma4`; fix that before anything else.

Run the shipped submission on real Gemma, one task at a time:

```bash
$PY -u scripts/run_experiment.py --name e6-gemma12b --backend openai --eval-set experiments/eval_sets/set30_a.json --quiet
```

The model name sent is the one the submission declares; pass `--model NAME` if the server uses a
different id. That server is not 4×L4, so also re-run E3 with the measured speeds scaled to what you
expect on the scorer:

```bash
$PY -u scripts/run_experiment.py --name e3-measured --eval-set experiments/eval_sets/set30_a.json --sim-prefill-tps 1500 --sim-decode-tps 30 --workers 4 --quiet
```

If an endpoint rejects vLLM-only request fields, set `SWE_OPENAI_PLAIN=1`.

## 5. Debugging one task

```bash
$PY scripts/submission_harness.py --task-id rich_3930 --auto --verify --sim gemma12b
```

```bash
$PY scripts/submission_harness.py --task-id rich_3930
```

The second form is the interactive REPL (`/auto`, `/diff`, `/status`, `/tools`, `/tree`, `/prompt`,
`/run`, `/cat`, `/verify`, `/reset`). `--submission-dir sample_submission_lean` selects the lean
variant. `--backend mock` runs the plumbing with no model.

## 6. Building the archive

Ship the directory that won. For the pipeline:

```bash
cd sample_submission && zip -r -X ../submission.zip . -x '*.DS_Store' '*/__pycache__/*' && cd ..
```

For the lean variant, replace `sample_submission` with `sample_submission_lean`. Then validate what
the scorer will unpack, not the working tree:

```bash
rm -rf /tmp/zcheck && unzip -q submission.zip -d /tmp/zcheck && $PY scripts/validate_submission.py /tmp/zcheck
```

```bash
$PY scripts/submission_harness.py --submission-dir /tmp/zcheck --dry-run --task-id fastapi_15589
```

Record the run and the score in `docs/AGENT_CLAUDE.md` §7 and `docs/IMPROVEMENT_PLAN.md` §9.
