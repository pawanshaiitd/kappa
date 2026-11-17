# kappa — Gemma 4 Developer Agent harness

A declarative agent submission for the Kaggle *Gemma 4 Developer Agent* competition, plus the local
harness used to compile, run and score it.

## Layout

| Path | What it is |
|---|---|
| `sample_submission/` | **the submission** — a `SequentialAgent` pipeline: locator → fixer → verifier, prompts, budgets |
| `sample_submission_lean/` | fallback variant: one agent, four tools, same budgets |
| `archive/submission_v2/` | the submission that hit the 18 h timeout, kept as an experiment baseline |
| `scripts/submission_harness.py` | compiles `agent.yaml` the way the scorer does and runs one task |
| `scripts/run_experiment.py` | runs a variant over a frozen eval set and reports a comparable score |
| `scripts/test_envs.py` | resolves each snapshot's own pins into a cached test venv |
| `scripts/run_gemini_benchmark.py` | Phase 2 verification (`verify_agent_patch`) and snapshot extraction |
| `experiments/eval_sets/` | frozen task-id lists, so results stay comparable |
| `experiments/prompt_variants/` | alternative system prompts for A/B experiments |
| `tests/` | compliance, time-safety and runtime tests (pipeline hand-off, sandbox isolation, simulated clock) |
| `RUNBOOK.md` | **every command, in order** — start here |
| `docs/AGENT_CLAUDE.md` | what the scorer does, what broke, pre-submit checklist |
| `docs/IMPROVEMENT_PLAN.md` | the experiment program and its log |
| `HARNESS_README.md` | upstream reference for the competition harness |

## Data not in this repository

Four directories are excluded for size (~27 GB). The code degrades gracefully without them, but you
need them for local scoring:

| Directory | Size | Needed for | How to restore |
|---|---|---|---|
| `snapshots/` | 20 GB | **everything** — one repo tarball per task | from the competition data bundle |
| `graphs/` | 403 MB | `get_code_neighbors`, `get_code_subgraph` | from the competition data bundle |
| `embeddings/` | 446 MB | `search_similar_code` | from the competition data bundle |
| `wheels/` | 27 MB | offline wheel cache for test venvs | from the competition data bundle |
| `.venv_cache/` | 4.5 GB | Phase 2 test environments | rebuilt locally, see below |

`tasks.jsonl` **is** included: every script reads it and it is only 1.9 MB.

Without `snapshots/` nothing can run. Without `graphs/`/`embeddings/` the three code-intelligence
tools return `NotIndexed` and the agent falls back to `grep`, which two live runs showed it already
prefers. `.venv_cache/` is never copied — rebuild it on the machine:

```bash
python scripts/run_experiment.py --name prep --prebuild-envs \
    --eval-set experiments/eval_sets/set30_matched.json
```

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install pyyaml pyarrow numpy networkx pytest
PYTHONPATH=. .venv/bin/python -m pytest tests/ -q
.venv/bin/python scripts/submission_harness.py --dry-run --task-id fastapi_15589
```

The dry run compiles the submission and prints "Validation clean" without calling any model.
Then follow [RUNBOOK.md](RUNBOOK.md).

## Building the submission archive

```bash
cd sample_submission && zip -r -X ../submission.zip . -x '*.DS_Store' '*/__pycache__/*' && cd ..
```

`agent.yaml` must sit at the **zip root**. Validate what the scorer will unpack, not the working tree:

```bash
unzip -q submission.zip -d /tmp/zcheck && \
  .venv/bin/python scripts/submission_harness.py --submission-dir /tmp/zcheck --dry-run --task-id fastapi_15589
```
