# AGENT_CLAUDE.md — Working Guide for the Gemma 4 Developer Agent Submission

Read this first before changing anything in `sample_submission/`. Commands are in `../RUNBOOK.md`. It describes what the scorer
actually runs, how this submission maps onto it, what was wrong in submission #1, and what to
improve next, module by module.

Source of truth for the scorer contract: `../HARNESS_README.md` (swegemma + adk-submission +
adk-eval-core). `../AGENT_PLAN.md` and `../AGENTS.md` describe the earlier Java MkPro harness; they
are design background only — none of their components run here.

---

## 1. What the scorer actually does

1. Unzips `submission.zip`, requires exactly one root config (`agent.yaml`) at the zip root.
2. `validate_single_declared_model` — every agent YAML must declare the **same** model, one of
   `gemma-4-31b-it-qat-w4a16-ct`, `gemma-4-12b-it-qat-w4a16-ct`, `gemma-4-e4b-it-qat-w4a16-ct`,
   `gemma-4-e2b-it-qat-w4a16-ct`.
3. Starts vLLM on 4×L4 (`max_model_len=32768`, `tool_call_parser=gemma4`, `reasoning_parser=gemma4`).
   **Every directory under `adapters/` is registered as a LoRA** and `enable_lora` is switched on.
4. `compile_submission()` builds the ADK agent tree from the YAML. Only what is reachable from
   `agent.yaml` is used. **Any compile error = every task fails = score 0.**
5. Container A per task: repo at `base_commit`, baseline commit, then a user message with the problem
   statement, hints, budget, environment rules, code-intel tool list and a depth-3 workspace layout.
6. Loop until `submit_patch()`, budget exhaustion, or 3 consecutive turns with no tool call. If the
   agent never submits, the harness still takes `git diff` of the working tree.
7. Container B: fresh repo, apply patch, reset and strip test/config files, apply the hidden
   `test_patch`, run pytest. Resolved iff exit 0 **and** every FAIL_TO_PASS/PASS_TO_PASS node passed.
8. Score = resolved / total.

Tool facts the prompts must respect:

| Tool | Limit / behaviour |
|---|---|
| `read_file` | max **150 lines / 10 000 chars** per call; returns `total_lines`, `is_truncated` |
| `run_command` | `bash -c` in `/workspace`, stdout+stderr each cut at 5 000 chars |
| `edit_file` | exact → whitespace-flexible → regex; must be **unique** unless `allow_multiple` |
| `submit_patch`, `get_status` | free — do not count toward `max_tool_calls` |
| graph tools | take **dotted symbol names**, not prose |

Edits to `tests/`, `test_*.py`, `conftest.py`, `pytest.ini`, `pyproject.toml`, `setup.cfg`, `tox.ini`
are stripped. Untracked files left in `/workspace` land in the patch → scratch belongs in `/tmp`.

---

## 2. Current design map (2026-11-17, submission #3)

```
sample_submission/
agent.yaml      swe_pipeline  SequentialAgent
├── locator.yaml   LlmAgent  tools: run_command, read_file, get_code_neighbors, search_similar_code
│                  sees the harness prompt; final reply stored as state["location"]
├── fixer.yaml     LlmAgent  include_contents: none   tools: read_file, edit_file, write_file, run_command
│                  instruction embeds {problem_description} {hints?} {location?}; reply -> state["fix_summary"]
└── verifier.yaml  LlmAgent  include_contents: none   tools: run_command, read_file, edit_file, submit_patch
                   instruction embeds {problem_description} {location?} {fix_summary?}; always submits
prompts/locator.md, fixer.md, verifier.md      configs/sampling.yaml (shared)
eval_config.yaml: timeout_seconds 45, max_tool_calls 22, max_time_minutes 3, max_turns 26
all agents: gemma-4-12b-it-qat-w4a16-ct, temp 0, max_output_tokens 2048, thinking_budget 512,
            include_thoughts false
```

Why this shape:

- **Context isolation.** Each step starts from its instruction plus one short report. The locator's
  grep output and file reads never reach the fixer. Per-request overhead is ~1 400–1 600 tokens per
  step (it was ~4 060), and the history stays far below the 14 336-token compaction threshold.
- **Memory is session state**, not `/tmp/notes.md`: `output_key` stores each step's fixed-format
  report and the next step's instruction pulls it in. Keys use the optional form (`{location?}`)
  because a step that ends without a text reply sets nothing, and a missing required key makes ADK
  raise mid-task.
- **Navigation is outline-first**: capped `grep` for definitions, a `grep` outline of a file instead
  of reading it, then one 60-line `read_file`. Graph tools are limited to the locator and to symbol
  names.
- **Import gate before submit**: `py_compile` on changed files plus `import PKG`. 9 of 21 failures in
  the Gemini baseline were patches that broke import or collection.
- **Agent YAMLs sit at the submission root** so `!include prompts/x.md` and `!include
  configs/sampling.yaml` need no `..` component.
- **Nothing else is in the directory.** The scorer's model discovery reads unreferenced YAMLs too,
  and `tests/test_harness_benchmark.py::test_only_live_files_are_shipped` enforces the file list.

`sample_submission_lean/` is the same model, sampling and budgets with a single `LlmAgent`
(`run_command`, `read_file`, `edit_file`, `submit_patch`). It exists because two things about the
pipeline cannot be confirmed without the real `adk-submission`: how the scorer nudges a
`SequentialAgent` root, and that `include_contents: none` keeps a step's own tool history. Ship the
pipeline only if it beats lean in the experiments (RUNBOOK §2) and
`test_real_adk_accepts_the_agent_tree` passes on a machine with `google-adk` installed.

The previous design (5 agents, 4 skills, 31B) is in `archive/submission_v2/`.

### Why submission #2 timed out

`max_time_minutes: 60` with tasks run one after another: 18 tasks at the cap use the whole 18 hours.
The official sample ships `max_time_minutes: 1`. The cap is now 3 minutes and
`scripts/validate_submission.py` rejects any value that could not finish 300 tasks.

---

## 3. What was wrong in submission #1 (score 0.0)

### 3.1 Skills failed ADK validation → compile error → every task failed  (verified, fixed)
ADK requires a skill `name` to be kebab-case **and equal to its directory name**. Verified against
the real loader (`google-adk==2.14.1`, `google.adk.skills._utils._load_skill_from_dir`): all four
skills were rejected with `name must be lowercase kebab-case`. Snake_case is accepted only behind the
`SNAKE_CASE_SKILL_NAME` feature flag, which is off by default. ADK also reads only `references/`,
`assets/` and `scripts/` inside a skill, so the `resources/` folders were silently ignored.
**Fixed:** directories and frontmatter renamed to `repo-navigation`, `code-intelligence`,
`precision-editing`, `test-verification`; `resources/` → `references/`; `agent.yaml` updated.
Re-verify after any change: `tests/test_harness_benchmark.py::test_skills_load_with_adk_rules`.

### 3.2 `!include ../prompts/x.md` is rejected as path traversal  (found while fixing, fixed)
All four sub-agent YAMLs used `instruction: !include ../prompts/analyzer.md` and
`generate_content_config: !include ../configs/sampling.yaml`. The submission loader blocks any
`!include` containing a `..` component (`PathTraversalError`), so each sub-agent would fail to
compile. **Fixed:** instruction and sampling are inlined in each sub-agent YAML. Guarded by
`test_no_parent_traversal_in_includes`.

### 3.3 Dummy LoRA adapters  (fixed)
`adapters/main_lora` and `adapters/tool_lora` were 217 KB placeholders: rank 4, layer 0 only,
`init_lora_weights: true`, untrained. Their presence forces `enable_lora` on a W4A16
compressed-tensors Gemma 4 model; if vLLM cannot load LoRA for that architecture, the inference
server never starts and every task scores 0. They added nothing even if they loaded.
**Fixed:** `adapters/` removed entirely. Guarded by `test_core_components_presence`.

### 3.4 Budget and latency mismatch  (tuned)
`thinking_budget: 4096` with `max_output_tokens: 16384` invites truncated `<|tool_call>` tags, and 31B
on 4×L4 is slow. `max_tool_calls: 35` was shared across the root agent and four sub-agent hops.
**Tuned:** output 6144 / thinking 2048 so turns are fast and tool calls close; `max_tool_calls` 70,
`max_time_minutes` 60, `timeout_seconds` 180.

### 3.5 Prompt did not match the tools  (rewritten)
The old system prompt pushed delegation and "under 8–10 turns" but never told the model to locate code
with `grep -n` before reading. With a 150-line `read_file` cap the model read the top of a 1 000-line
file and then invented `old_string` — exactly the two `FileEditError`s seen on
`fastapi/dependencies/utils.py` in the manual run. **Rewritten** as a concrete six-step loop:
grep → read the exact range → minimal fix → edit with verbatim `old_string` → `/tmp` repro plus one
targeted pytest → clean up and submit, with explicit `FileEditError` recovery and a "submit something"
rule at ~70 % budget.

---

## 4. What to try next

The experiment queue (E1–E6), accept criteria and commands are in `../RUNBOOK.md` §2–§4; background
on each module is in `IMPROVEMENT_PLAN.md`. Order: fixed-rig baseline → pipeline vs lean → simulated
Gemma clock → thinking budget and repo notes → confirm on the second eval set → real Gemma endpoint.

Still open after this iteration:

- **Real Gemma speed and tool-parser behaviour.** Nothing here has run on Gemma. The simulated clock
  uses assumed speeds until `--probe-endpoint` measures them.
- **Per-step sampling.** All three steps share one sampling file; the fixer may deserve more thinking
  than the locator and verifier.
- **LoRA.** Traces in `results/experiments/*/traces/` are the raw material for a tool-discipline
  adapter. No adapter goes in a zip until it loads under vLLM with the W4A16 base.

---

## 5. Local testing

`scripts/submission_harness.py` compiles a submission the way the scorer does and runs one task;
`scripts/run_experiment.py` runs a frozen eval set and prints a comparable summary. What the rig
reproduces:

| Scorer behaviour | Local rig |
|---|---|
| `!include` rules, single model, allowed extensions | same checks, plus unreferenced YAMLs |
| `LlmAgent`, `SequentialAgent`, `LoopAgent`, `agent_tool` | all executed; `ParallelAgent` runs its branches in turn |
| `output_key`, `include_contents`, `{key}` / `{key?}` templating | reproduced; a missing required key ends the task as `state_key_error` |
| Other agents' events shown as "For context: [agent] said" | reproduced |
| Container A: repo importable, test deps installed, private `/tmp` | per-task venv on `PATH`, `PYTHONPATH`, `/tmp` rewritten to a per-task directory |
| `.git/info/exclude` before the baseline commit | written (it was missing: a 29 MB patch) |
| 32 768-token window | a request over the limit ends the task as `context_overflow` |
| Events compaction at 14 336 tokens | emulated with a mechanical summary; counted per task |
| `max_turns` | counts every model request, sub-agents included |
| Nudges | sent after every invocation that ends without `submit_patch`; limit of 3 without a tool call |
| Gemma speed on 4×L4 | `--sim` / `--sim-*-tps` project the task clock from token counts |

Not reproduced: the `gemma4` tool-call parser (Gemini's function calling is more forgiving), ADK's
real compaction summary (an LLM call), Docker resource limits, and whatever the real scorer does that
`HARNESS_README.md` does not describe. A pass on Gemini is evidence about prompts and workflow, not
about Gemma.

The older `scripts/interactive_harness.py`, `scripts/run_gemini_benchmark.py` (agent loop),
`scripts/inference.py` and `scripts/benchmark_harness.py` predate this and read only a flat prompt and
`tool_schemas.json` from `archive/submission_v2/`. Do not use them to judge a submission.

---

## 6. Pre-submit checklist

- [ ] `PYTHONPATH=. .venv/bin/python -m pytest tests/ -q` passes.
- [ ] `scripts/validate_submission.py <dir>` prints `[PASS]` (model, size, time safety, compile).
- [ ] E3 for the variant being shipped projects ≤ 12 h at 300 tasks with `compactions=0`.
- [ ] On a machine with `google-adk`: `test_real_adk_accepts_the_agent_tree` passes, not skips.
- [ ] No `adapters/`, no file in the directory that `agent.yaml` does not reach.
- [ ] No `{word}` in any prompt except `problem_description`, `hints?`, `location?`, `fix_summary?`.
- [ ] `max_output_tokens` ≤ 2048; no `thinking_level`.
- [ ] Zip built from **inside** the submission directory, then the extracted copy validated
      (`../RUNBOOK.md` §6).
- [ ] Change and score recorded in §7.

---

## 7. Submission log

| # | Date | Change | Score |
|---|---|---|---|
| 1 | ≤ 2026-11-13 | 5-agent tree, 4 snake_case skills (compile failure), `!include ../` traversal, dummy LoRAs, 31B | 0.0 |
| 2 | 2026-11-13 | kebab-case skills + `references/`; sub-agent instructions and sampling inlined; `adapters/` removed; concrete 6-step system prompt; sub-agent prompts rewritten with fixed report formats; sampling 6144/2048; budgets 70 calls / 60 min / 180 s; same 31B model and same 5-agent topology | **timed out at 18 h** (60 min per-task cap) |
| 3 | *not yet submitted* | 12B; `SequentialAgent` locator → fixer → verifier with state hand-off; 3 min / 22 calls / 26 turns; output 2048 / thinking 512 / thoughts not kept; import gate before submit; dead files removed. Lean single-agent variant kept as fallback | *pending experiments E1–E5* |
