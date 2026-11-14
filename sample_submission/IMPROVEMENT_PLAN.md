# Improvement Plan — per module, with runnable experiments

Companion to `AGENT_CLAUDE.md` (what the scorer does, what was broken, how the submission is built).
This document is the **what to try next**, in order, with a command, a metric and a decision rule for
each step. Submission #2 fixed the things that made the score 0; everything below is about moving the
score up from whatever #2 lands on.

**The rule that governs all of it:** change one variable, run the same frozen task set, compare the
resolution rate *and the failure buckets*. The buckets matter more than the rate at this sample size —
"12 tasks never attempted an edit" tells you what to fix; "rate 0.17" does not.

---

## 0. Measurement first (do this before any tuning)

Without a repeatable local score, every change below is guesswork.

### 0a. Local Phase 2 only scores some tasks — always audit first

Run each task's own **reference** patch through Phase 2: if the official fix fails, this machine
cannot score that task and it must be excluded, not blamed on the agent.

```bash
.venv/bin/python scripts/run_experiment.py --validate-eval-set \
  --out-eval-set experiments/eval_sets/all_scorable.json
```

The first audit found only 58 of 129 scorable, because `verify_agent_patch` ran every repo against
the harness's own `.venv`. `scripts/test_envs.py` now resolves each snapshot's own pins
(`requirements-tests.txt` + `-r` includes + `-e .[extras]` expanded from
`[project.optional-dependencies]` + PEP 621/poetry runtime deps) and caches a venv keyed by a hash
of that set, so tasks sharing pins share an environment. Inspect with:

```bash
.venv/bin/python scripts/test_envs.py --snapshot snapshots/<id>.tgz --repo <owner/name>
.venv/bin/python scripts/test_envs.py --list
```

`SWE_TEST_ENV=shared` restores the old single-venv behaviour. Every result row records which
`test_env` produced it, so a score is never silently attributed to the wrong environment.

```bash
# one-time: freeze a repo-stratified 30-task eval set (already created, seed 7)
.venv/bin/python scripts/run_experiment.py --make-eval-set 30 --from-pool experiments/eval_sets/all_scorable.json --match-distribution --eval-set experiments/eval_sets/set30_matched.json
```

```bash
# the baseline every variant is measured against
.venv/bin/python scripts/run_experiment.py --name baseline --eval-set experiments/eval_sets/set30_matched.json
```

```bash
# after a few experiments
.venv/bin/python scripts/run_experiment.py --compare
```

`scripts/run_experiment.py` copies the submission to a temp directory, applies the override, validates
that the variant still compiles (it aborts if not), runs Phase 1 + Phase 2 per task, and writes
`results/experiments/<name>/{summary.json,task_results.jsonl,patches/}`. Each run reports:

| Field | What it tells you |
|---|---|
| `resolution_rate` | the score |
| `failure_buckets` | `no_edit_attempted` / `edits_made_but_no_diff` / `patch_did_not_apply` / `tests_failed` |
| `end_reasons` | `submit_patch` / `nudge_limit` / `time_budget_exhausted` / `tool_call_budget_exhausted` |
| `median_first_edit_turn` | how long localisation takes — the single best efficiency signal |
| `tool_errors` | e.g. `edit_file:FileEditError` counts, the symptom that cost #1 its manual run |
| `failing_commands` | the actual shell commands that failed, grouped — the only way to turn a failure *rate* into a prompt change |
| `empty_patches` | how often the agent produced nothing at all |
| `test_envs` | which cached environment scored each task |

### Reading the buckets

| Dominant bucket | What it means | Which section to go to |
|---|---|---|
| `no_edit_attempted` + `end_reason: time/tool_call budget` | the agent never finds the code | §1 prompt, §6 tools, §4 budgets |
| `no_edit_attempted` + `end_reason: nudge_limit` | the model stops emitting tool calls — parser or prompt problem | §1 prompt, §3 sampling |
| high `edit_file:FileEditError` | `old_string` invented rather than copied | §1 prompt |
| `tests_failed` | localisation works, the fix is wrong | §1 prompt step 3, §5 model |
| `edits_made_but_no_diff` | edits landed outside tracked files, or in protected files | §1 prompt hard rules |
| `patch_did_not_apply` | patch format problem — investigate directly, should be rare |

**Caveat that applies to every experiment below:** the only backend available here is Gemini (and the
key currently has an IP restriction — see `AGENT_CLAUDE.md` §5). Gemini's function calling is far more
forgiving than vLLM's `gemma4` tool parser, so Gemini measures **prompt and workflow quality**, not
Gemma's tool-call reliability. Sampling (§3) and model choice (§5) are only meaningful against a real
Gemma endpoint:

```bash
.venv/bin/python scripts/run_experiment.py --name <n> --backend openai \
  --api-base http://127.0.0.1:8000/v1 --model gemma-4-31b-it-qat-w4a16-ct \
  --eval-set experiments/eval_sets/set30_matched.json
```

---

## 0b. What two live runs actually showed

Measured tool usage, not speculation:

| Tool | run A (`fastapi_15589`) | run B (`fastapi_13537`) |
|---|---|---|
| `run_command` | 10 (**6 errors**) | 22 (**14 errors + 2 timeouts**) |
| `read_file` | 4 | 4 |
| `edit_file` | 1 | 3 |
| `write_file` | 2 (polluted the patch) | 0 |
| `load_skill` | **0** | **0** |
| graph tools | **0** | **0** |

Consequences, already applied:

1. **Skills never fired.** A skill is opt-in and the model never opted in, so it contributes only its
   frontmatter line. Anything that must happen reliably now lives in `prompts/system.md`: the
   working-notes procedure, the fix-shape rule, the pre-submit checks and the tool-error table.
   Skills remain as on-demand reference only.
2. **Graph tools never fired** either, while their declarations are sent on every request. §F2 is now
   an evidence-backed change rather than a guess.
3. **`run_command` is the whole game and fails 60–64% of the time.** `failing_commands` in the
   summary now captures which ones, so §A2's few-shot can target the real mistakes.

## 1. Module A — the system prompt  *(highest leverage, cheapest)*

`prompts/system.md` is the only thing every task depends on and the only one you can iterate on in
minutes. Expect the largest single gain here.

### A1. Short prompt vs long prompt
The current prompt is ~90 lines. Small instruction-tuned models often follow a short prompt better,
and a long one eats context that the trajectory needs.

```bash
# write a ~40-line variant keeping only: the 6 steps, the verbatim-old_string rule,
# never-touch-tests, scratch-in-/tmp, and submit-something-at-70%
.venv/bin/python scripts/run_experiment.py --name prompt-short \
  --system-prompt prompts/system_short.md --eval-set experiments/eval_sets/set30_matched.json
```
**Decide:** keep whichever has the higher rate; on a tie keep the shorter one (it leaves more context
for the trajectory). Watch `median_first_edit_turn` as the tiebreaker.

### A2. Few-shot a full grep → read → edit sequence
Tool discipline responds better to one worked example than to instructions. Add a single compact
transcript showing: grep with `--include=*.py`, `read_file` around the hit, `edit_file` with
`old_string` copied from that output, `/tmp` repro, targeted pytest, `submit_patch`.

```bash
.venv/bin/python scripts/run_experiment.py --name prompt-fewshot \
  --system-prompt prompts/system_fewshot.md --eval-set experiments/eval_sets/set30_matched.json
```
**Decide:** adopt if `edit_file:FileEditError` drops or `median_first_edit_turn` falls. This is the
targeted fix for the failure seen in #1's manual run.

### A3. Order of the checklist
Some models comply better when the imperative list is last and the context is first. Produce a variant
that moves "Hard rules" and "Budget" above "The loop you must follow".
**Decide:** adopt only on a clear rate improvement; this is a low-prior experiment, run it late.

---

## 2. Module B — agent topology  *(largest structural question)*

Four `agent_tool`s mean every delegation is a full sub-agent run with its own thinking budget, and
sub-agents see **only the request string** — not the problem statement, not the root's findings — so
they re-explore from scratch on a shared tool-call budget.

### B1. Single agent vs the 5-agent tree  ← run this immediately after the baseline
```bash
.venv/bin/python scripts/run_experiment.py --name no-subagents --no-subagents \
  --eval-set experiments/eval_sets/set30_matched.json
```
**Decide:** if the single agent matches or beats the tree, ship the single agent — it has strictly
fewer failure modes. Compare `median_tool_calls` and `median_seconds`, not just the rate: equal rate at
half the budget is a win, because it leaves room for correction attempts.

### B2. Keep only the analyzer
If B1 says delegation helps, the plausible reason is context isolation during exploration: the
analyzer's `read_file` output never enters the root history. Test that specific claim by keeping only
`code_analyzer_agent`. Requires a hand-edited `agent.yaml` variant (the runner's `--no-subagents`
removes all four); copy the submission, delete three `agent_tool` blocks, and point
`--submission-dir` at the copy.
**Decide:** adopt the smallest topology that holds the rate.

### B3. Drop `write_file` from the root agent
SWE-bench fixes almost never need a new file, and any file created under `/workspace` lands in the
patch. Removing it eliminates a whole class of patch pollution.
**Decide:** adopt unless the rate drops; verify `empty_patches` and `edits_made_but_no_diff` do not rise.

---

## 3. Module C — sampling  *(only meaningful against real Gemma)*

`configs/sampling.yaml`, currently temp 0.1 / top_p 0.95 / top_k 64 / out 6144 / thinking 2048.

### C1. Thinking-budget sweep
Reasoning helps localisation but costs wall clock, and wall clock is the binding constraint on 4×L4.
```bash
for tb in 0 1024 2048 4096; do
  .venv/bin/python scripts/run_experiment.py --name think-$tb --thinking-budget $tb \
    --backend openai --api-base http://127.0.0.1:8000/v1 \
    --eval-set experiments/eval_sets/set30_matched.json
done
```
**Decide:** pick the knee — the smallest budget whose rate is within noise of the best. Watch
`end_reasons` for `time_budget_exhausted` rising as the budget grows.

### C2. Truncation guard: `max_output_tokens`
A long thought followed by a large `edit_file` is what cuts the `<|tool_call>` tag. If `tool_errors`
or the nudge counts show truncation, try 4096.
```bash
.venv/bin/python scripts/run_experiment.py --name out-4096 --max-output-tokens 4096 \
  --backend openai --api-base http://127.0.0.1:8000/v1 --eval-set experiments/eval_sets/set30_matched.json
```
**Decide:** adopt if `nudges` falls without the rate dropping.

### C3. Determinism and thought retention
`--temperature 0.0` for reproducibility across runs, and separately `include_thoughts: false`, which
keeps reasoning on but strips thought parts from the outgoing history, freeing context on long
trajectories (hand-edit `configs/sampling.yaml` for the latter).
**Decide:** prefer temperature 0.0 if the rate is unchanged — it makes every later experiment cleaner.

---

## 4. Module D — budgets

`eval_config.yaml`: 70 tool calls / 60 min / 180 s commands / 60 turns. These were reasoned, not
measured.

### D1. Set them from the baseline's own numbers
Read `median_tool_calls`, `median_seconds` and `median_first_edit_turn` from the baseline summary.
If the p90 of tool calls is well under 70, the limit is not binding and raising it buys nothing; if
`end_reasons` is dominated by `time_budget_exhausted`, cut thinking (§C1) before cutting tool calls —
exploration is what finds the bug.

### D2. Does a tighter tool budget help?
A smaller budget plus the "submit at 70 %" rule can beat a larger one by forcing a decision before the
clock runs out.
```bash
.venv/bin/python scripts/run_experiment.py --name calls-40 --max-tool-calls 40 \
  --eval-set experiments/eval_sets/set30_matched.json
```
**Decide:** adopt if the rate holds and `empty_patches` falls.

---

## 5. Module E — model choice  *(probably the biggest single lever)*

31B is the most capable and the slowest; under a wall-clock budget, **12B may resolve more tasks**.
This cannot be answered from here — it needs the real models on real hardware.

```bash
# serve each in turn, then:
.venv/bin/python scripts/run_experiment.py --name model-31b \
  --declare-model gemma-4-31b-it-qat-w4a16-ct --backend openai \
  --api-base http://127.0.0.1:8000/v1 --eval-set experiments/eval_sets/set30_matched.json
.venv/bin/python scripts/run_experiment.py --name model-12b \
  --declare-model gemma-4-12b-it-qat-w4a16-ct --backend openai \
  --api-base http://127.0.0.1:8000/v1 --eval-set experiments/eval_sets/set30_matched.json
```
**Decide:** on resolution rate at the same wall-clock budget. Expect 12B to show more
`edit_file:FileEditError` and more nudges but to get further per minute. e4b/e2b are likely too weak at
multi-step tool use — test only if `end_reasons` on 12B is still dominated by time exhaustion.

Note: whichever wins, **every agent YAML must declare it** — `--declare-model` handles the whole tree,
and `test_single_declared_model_across_tree` guards it.

---

## 6. Module F — code intelligence tools

`search_similar_code`, `get_code_neighbors`, `get_code_subgraph` need **dotted symbol paths**, so they
cannot find code from a prose description. If the model keeps calling them with prose, they are pure
budget waste.

### F1. Measure their use and their success
```bash
grep -ho '"search_similar_code": [0-9]*' results/experiments/baseline/task_results.jsonl
python - <<'PY'
import json
rows=[json.loads(l) for l in open("results/experiments/baseline/task_results.jsonl")]
use=sum(r.get("tool_uses",{}).get("search_similar_code",0) for r in rows)
err=sum(v for r in rows for k,v in (r.get("tool_errors") or {}).items() if "search_similar" in k)
print("search_similar_code calls:",use,"errors:",err)
PY
```

### F2. Drop them
```bash
.venv/bin/python scripts/run_experiment.py --name no-graph --no-graph-tools \
  --eval-set experiments/eval_sets/set30_matched.json
```
**Decide:** if the rate is unchanged, drop them — a shorter tool list improves tool-call accuracy on
small models, which matters more under the `gemma4` parser than it does with Gemini.

### F3. Skills
Same question for the four skills (they cost a frontmatter listing in every system prompt plus the
`load_skill` tools).
```bash
.venv/bin/python scripts/run_experiment.py --name no-skills --no-skills \
  --eval-set experiments/eval_sets/set30_matched.json
```
**Decide:** keep skills only if they earn their rate. Check whether `load_skill` is ever called at all
(`tool_uses`); a skill the model never loads is pure overhead.

---

## 7. Module G — LoRA adapters  *(last, and only with real training)*

Only after the prompt, topology and model questions are settled. Requirements: rank ≤ 128,
`.safetensors` only, total unpacked < 3 GiB, and — the part that broke #1 — **proven to load under
vLLM with the quantized base model** before it goes near a submission. A placeholder adapter is worse
than none, because its mere presence switches `enable_lora` on. Train on successful trajectories from
the experiments above (`results/experiments/*/task_results.jsonl` plus the patches) so the adapter
learns this harness's tool-call format.

---

## 8. Suggested order of work

| # | Experiment | Backend needed | Why first |
|---|---|---|---|
| 1 | `baseline` | Gemini ok | nothing is comparable without it |
| 2 | `no-subagents` (§B1) | Gemini ok | biggest structural simplification available |
| 3 | `prompt-fewshot` (§A2) | Gemini ok | directly targets the observed `FileEditError` mode |
| 4 | `prompt-short` (§A1) | Gemini ok | cheap, often wins on small models |
| 5 | `no-graph` / `no-skills` (§F) | Gemini ok | shrinks the tool surface |
| 6 | `model-31b` vs `model-12b` (§E) | **real Gemma** | likely the biggest lever, needs the GPUs |
| 7 | thinking sweep (§C1) | **real Gemma** | only meaningful with real latency |
| 8 | budgets from data (§D1) | **real Gemma** | depends on 6 and 7 |
| 9 | LoRA (§G) | **real Gemma** | depends on everything |

Steps 1–5 are runnable on this laptop once the API key's IP restriction is cleared. Steps 6–9 need the
competition hardware or an equivalent vLLM host.

---

## 9. Experiment log

Record every run here, including the ones that made things worse — a negative result stops the next
person repeating it.

| Date | Experiment | Eval set | Backend / model | Rate | Dominant bucket | Verdict |
|---|---|---|---|---|---|---|
| 2026-11-13 | `smoke-mock` | 2 ids | mock | 0.000 | `no_edit_attempted` | plumbing check only, not a result |
| 2026-11-13 | `baseline` (attempt 1) | set30_matched | gemini-2.5-flash | — | `crashed` x30 | **void**: API key IP restriction blocked every call. Discarded; not a result |
| | `baseline` | set30_matched | gemini-2.5-flash | | | *pending* |
