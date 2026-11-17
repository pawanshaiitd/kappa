#!/usr/bin/env bash
# Gemma 4 31B suite on set30_a. Mirrors the 12B runs (e2-pipeline, e2-lean, e4-two-step,
# e4-pipeline-calls30) so results are directly comparable with `--compare`.
# Runs the experiments one after another (4 workers each) so the 31B endpoint and this
# machine's test envs are not oversubscribed while the 12B runs are still going.
#
# Outputs:
#   results/experiments/g31-*/      summary.json, task_results.jsonl, patches/, traces/
#   results/logs/g31-*.log          full console log per experiment
#   results/logs/g31-suite.log      suite progress (start/end/exit code per experiment)
#   results/logs/g31-probe.json     endpoint probe (served model, measured tps)
#   results/logs/g31-compare.txt    `--compare` + `--analyze` output, written at the end
set -u
cd "$(dirname "$0")/.."

PY=.venv/bin/python
API=http://10.120.12.55:8000/v1
MODEL=google/gemma-4-31B-it-qat-w4a16-ct
DECLARE=gemma-4-31b-it-qat-w4a16-ct
SET=experiments/eval_sets/set30_a.json
WORKERS=${WORKERS:-4}
LOGS=results/logs
SUITE_LOG=$LOGS/g31-suite.log
export SWE_API_BASE=$API

COMMON=(--backend openai --api-base "$API" --model "$MODEL" --declare-model "$DECLARE"
        --eval-set "$SET" --workers "$WORKERS" --quiet --resume)

log() { echo "$(date '+%F %T') $*" | tee -a "$SUITE_LOG"; }

run() {
  local name=$1; shift
  log "START $name  args: $*"
  "$PY" -u scripts/run_experiment.py --name "$name" "${COMMON[@]}" "$@" > "$LOGS/$name.log" 2>&1
  local rc=$?
  log "END   $name  exit=$rc"
}

log "=== 31B suite: model=$MODEL endpoint=$API set=$SET workers=$WORKERS"
"$PY" scripts/submission_harness.py --probe-endpoint --api-base "$API" --model "$MODEL" \
  > "$LOGS/g31-probe.json" 2>&1
log "probe written to $LOGS/g31-probe.json"

# E2 - quality, 30-minute cap (RUNBOOK §2)
run g31-e2-pipeline --max-time-minutes 30
run g31-e2-lean --submission-dir sample_submission_lean --max-time-minutes 30
# E4 - same variants as the 12B e4 runs
run g31-e4-two-step --submission-dir experiments/variants/two_step
run g31-e4-pipeline-calls30 --max-tool-calls 30 --max-turns 36 --max-time-minutes 8
# E3 - shipped budgets on the 31B clock measured from this endpoint
run g31-e3-pipeline-measured --sim-prefill-tps 636 --sim-decode-tps 10.6

{
  "$PY" scripts/run_experiment.py --compare
  for n in g31-e2-pipeline g31-e2-lean g31-e4-two-step g31-e4-pipeline-calls30 g31-e3-pipeline-measured; do
    echo; echo "================ $n"; "$PY" scripts/run_experiment.py --analyze "$n"
  done
} > "$LOGS/g31-compare.txt" 2>&1
log "=== suite done; comparison in $LOGS/g31-compare.txt"
