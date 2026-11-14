# Runbook — running the benchmark on a server

Everything here is runnable without the Kaggle hardware. Step 0 needs no model; steps 3+ need either
a Gemini key or a local OpenAI-compatible endpoint serving Gemma 4.

Replace `$PY` with your interpreter (`.venv/bin/python` locally).

---

## 0. One-time setup on the machine

```bash
export GOOGLE_API_KEY="..."      # or GEMINI_API_KEY; not needed for steps 1-2
$PY -m pip install -r <your deps>   # pyyaml, pyarrow, numpy, networkx, pytest
```

Check the key actually works from **that machine's** IP before starting a sweep — an API key with an
IP-address restriction fails every call with HTTP 403, and a sweep then reports a meaningless 0.000:

```bash
curl -s -X POST "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=$GOOGLE_API_KEY" \
  -H 'Content-Type: application/json' -d '{"contents":[{"role":"user","parts":[{"text":"say ok"}]}]}'
```

---

## 1. Find which tasks the machine can score  *(no model calls)*

Runs each task's own **reference** patch through Phase 2. If the official fix fails, that task cannot
be scored there and must be excluded rather than blamed on the agent. Takes ~45-90 min for 129 tasks,
mostly building test venvs.

```bash
$PY scripts/run_experiment.py --validate-eval-set \
    --out-eval-set experiments/eval_sets/all_scorable.json
```

Expect roughly 95/129 scorable. A much lower number means the test environments are not building —
check with `$PY scripts/test_envs.py --list`.

## 2. Draw an eval set in the real repo proportions  *(instant)*

```bash
$PY scripts/run_experiment.py --make-eval-set 30 \
    --from-pool experiments/eval_sets/all_scorable.json --match-distribution \
    --eval-set experiments/eval_sets/set30_matched.json
```

Proportions come from the full task file (52% fastapi / 37% rich / 10% requests), not from the
scorable pool, so the machine's own limitations are not baked into every later comparison.

## 3. Warm the test venvs  *(no model calls)*

Do this before spending tokens: building a venv mid-sweep is slow, and a transient index failure
would otherwise cost that task.

```bash
$PY scripts/run_experiment.py --name prep --prebuild-envs \
    --eval-set experiments/eval_sets/set30_matched.json
```

Must end with `All environments ready.`

## 4. The baseline  — **this is the command you asked for**

```bash
$PY -u scripts/run_experiment.py --name baseline \
    --eval-set experiments/eval_sets/set30_matched.json --quiet
```

~2-3 hours for 30 tasks against `gemini-2.5-flash`. Add `-u` (unbuffered) when backgrounding, or the
log looks empty until it finishes. Results land in `results/experiments/baseline/`
(`summary.json`, `task_results.jsonl`, `patches/`).

Against real Gemma on a vLLM host instead:

```bash
$PY -u scripts/run_experiment.py --name baseline-31b \
    --backend openai --api-base http://127.0.0.1:8000/v1 \
    --model gemma-4-31b-it-qat-w4a16-ct \
    --eval-set experiments/eval_sets/set30_matched.json --quiet
```

## 5. Read the result

```bash
$PY scripts/run_experiment.py --compare
```

Judge by `failure_buckets` and `failing_commands`, not the rate alone — at 30 tasks a one- or
two-task difference is noise. Record every run in `sample_submission/IMPROVEMENT_PLAN.md` §9,
including negative results.

## 6. The experiment queue

Run one at a time, each against the same eval set, comparing to `baseline`:

```bash
# topology: four AgentTool hops share one budget and cannot see the problem statement
$PY -u scripts/run_experiment.py --name no-subagents --no-subagents \
    --eval-set experiments/eval_sets/set30_matched.json --quiet

# prompt: a worked grep->read->edit transcript, aimed at the FileEditError mode
$PY -u scripts/run_experiment.py --name prompt-fewshot \
    --system-prompt experiments/prompt_variants/system_fewshot.md \
    --eval-set experiments/eval_sets/set30_matched.json --quiet

# prompt: 31 lines instead of 130 - small models often follow short prompts better
$PY -u scripts/run_experiment.py --name prompt-short \
    --system-prompt experiments/prompt_variants/system_short.md \
    --eval-set experiments/eval_sets/set30_matched.json --quiet

# surfaces that two live runs never once used
$PY -u scripts/run_experiment.py --name no-graph --no-graph-tools \
    --eval-set experiments/eval_sets/set30_matched.json --quiet
$PY -u scripts/run_experiment.py --name no-skills --no-skills \
    --eval-set experiments/eval_sets/set30_matched.json --quiet

# model choice - real Gemma only; likely the single biggest lever
$PY -u scripts/run_experiment.py --name model-12b \
    --declare-model gemma-4-12b-it-qat-w4a16-ct \
    --backend openai --api-base http://127.0.0.1:8000/v1 \
    --eval-set experiments/eval_sets/set30_matched.json --quiet
```

Gemini measures prompt and workflow quality only: its function calling is far more forgiving than
vLLM's `gemma4` tool parser, so sampling and model decisions need a real Gemma endpoint.

---

## Debugging a single task

```bash
$PY scripts/submission_harness.py --dry-run --task-id <id>                  # compile + validate only
$PY scripts/submission_harness.py --task-id <id> --auto --backend mock      # loop, no API
$PY scripts/submission_harness.py --task-id <id> --auto --verify            # one real run
$PY scripts/submission_harness.py --task-id <id> --verify-patch <file>.patch # Phase 2 only
$PY scripts/submission_harness.py --task-id <id>                            # interactive REPL
$PY scripts/test_envs.py --snapshot snapshots/<id>.tgz --repo <owner/name>  # resolved pins
```

`--test-env shared` on either script restores the old single-venv behaviour, for comparison.

## Before submitting

```bash
PYTHONPATH=. $PY -m pytest tests/ -q                       # 21 tests
$PY scripts/submission_harness.py --dry-run --task-id <id>  # "Validation clean"
cd sample_submission && zip -r -X ../submission.zip . -x '*.DS_Store' '*/__pycache__/*' && cd ..
unzip -q submission.zip -d /tmp/zcheck && \
  $PY scripts/submission_harness.py --submission-dir /tmp/zcheck --dry-run --task-id <id>
```

The last line matters: it validates what the scorer will actually unpack, not the working tree.
Full checklist in `sample_submission/AGENT_CLAUDE.md` §6.
