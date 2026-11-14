# AGENT_CLAUDE.md — Working Guide for the Gemma 4 Developer Agent Submission

Read this first before changing anything in `sample_submission/`. It describes what the scorer
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

## 2. Current design map (2026-11-13, submission #2)

```
agent.yaml  (mkpro_coordinator, LlmAgent, gemma-4-31b-it-qat-w4a16-ct)
├── instruction: !include prompts/system.md        (concrete 6-step loop)
├── generate_content_config: !include configs/sampling.yaml
│     temp 0.1, top_p 0.95, top_k 64, max_output_tokens 6144,
│     seed 20261113, thinking_budget 2048, include_thoughts true
├── tools: run_command, read_file, edit_file, write_file, get_status, submit_patch,
│          get_code_neighbors, search_similar_code, get_code_subgraph
├── agent_tool → sub_agents/code_analyzer.yaml      read + run_command + graph tools
├── agent_tool → sub_agents/architect_planner.yaml  read + run_command + graph + status
├── agent_tool → sub_agents/precision_editor.yaml   read_file + edit_file  (no write_file)
├── agent_tool → sub_agents/targeted_verifier.yaml  run_command + read + status
└── skills: repo-navigation, code-intelligence, precision-editing, test-verification
eval_config.yaml: timeout_seconds 180, max_tool_calls 70, max_time_minutes 60, max_turns 60
```

Sub-agent YAMLs carry their instruction and sampling **inline**, not via `!include ../prompts/x.md`
— see §3.2. `prompts/analyzer.md`, `planner.md`, `editor.md`, `verifier.md` remain as the editable
source; if you change one, re-run the generator snippet in §6 so the YAML stays in sync.

### Live vs dead files

| Path | Used by scorer? | Note |
|---|---|---|
| `agent.yaml`, `eval_config.yaml`, `configs/`, `sub_agents/`, `prompts/system.md` | **Yes** | the real design |
| `skills/*` | **Yes** | fixed in #2; validated against the real ADK loader |
| `prompts/analyzer.md` … `verifier.md` | No (inlined into the YAML) | keep as the source to edit |
| `rules/`, `memory/`, `asr/`, `tools/*.md`, `tools/tool_schemas.json` | No | not reachable from `agent.yaml`; only local scripts read `tool_schemas.json` |
| `AGENT.md`, `AGENTS.md`, `AGENT_PLAN.md`, `README.md`, `build_*.py` | No | docs and generators; harmless |
| `adapters/` | **removed in #2** | any subdir there switches LoRA on in vLLM |

Guidance that lives only in `rules/*.md` never reaches the model. The parts that mattered
(test isolation, patch hygiene, editing discipline) are now in `prompts/system.md` and the skills.

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

## 4. Improvement roadmap, module by module

Full version with runnable commands, metrics and decision rules: **`IMPROVEMENT_PLAN.md`**, driven by
`scripts/run_experiment.py` (frozen eval set, per-variant resolution rate, failure buckets). Summary:

### Module A — prompt (`prompts/system.md`)
Highest leverage and cheapest to iterate.
- Try a **shorter** variant (~40 lines). Small models follow short prompts better; the current one is
  ~90 lines and may itself crowd the context.
- Add two or three worked examples of a grep → read → edit sequence (few-shot beats instruction for
  tool discipline).
- Experiment with ordering: some models follow a numbered checklist better when it comes last.

### Module B — agent topology (`agent.yaml`, `sub_agents/`)
- Baseline to beat: **single agent, no AgentTools.** Each hop is a full sub-agent run with its own
  thinking budget, and sub-agents see only the request string, so they re-explore. Measure the
  coordinator with and without the four sub-agents on the same tasks.
- If delegation does help, keep only `code_analyzer_agent` — read-only exploration is where context
  isolation genuinely pays (its `read_file` output never enters the root history).
- `write_file` on the root agent is a patch-pollution risk and is almost never needed for SWE-bench.
  Consider dropping it.

### Module C — sampling (`configs/sampling.yaml`)
- Sweep `thinking_budget` at 0 / 1024 / 2048 / 4096. Reasoning helps localisation but costs wall clock,
  and budget is the binding constraint.
- Try `temperature: 0.0` for determinism, and compare `include_thoughts: false` — dropping thought
  parts from history frees context on long trajectories.
- Never set `thinking_level` with vLLM: it maps to OpenAI `reasoning_effort` and is dropped.

### Module D — budgets (`eval_config.yaml`)
- Instrument first: record median turns and seconds to the first `edit_file` per task, then set
  budgets from the measurement rather than guessing.
- If tasks run out of time mid-exploration, cut thinking before cutting tool calls.

### Module E — model choice
- 31B is the most capable and the slowest. **12B may resolve more tasks per unit of wall clock.** Run
  the same task set on 31B and 12B before deciding; this is likely the single biggest lever.
- e4b/e2b are probably too weak at multi-step tool use; try only if time is clearly the bottleneck.

### Module F — code intelligence tools
- Verify they earn their place. `search_similar_code` needs a dotted symbol path, so it cannot find
  code from a prose description; if the model keeps calling it with prose, remove the three graph
  tools and let it rely on `grep`.

### Module G — LoRA adapters
- Only after the above. Rank ≤ 128, `.safetensors` only, total unpacked < 3 GiB, and it must be proven
  to load under vLLM with the quantized base model before it goes anywhere near a submission.

---

## 5. Local testing

### `scripts/submission_harness.py` — use this one
It compiles the submission the way the scorer does: resolves `!include` with the same restrictions,
enforces the single-model rule, exposes only each agent's declared tools, turns every `agent_tool`
into a real nested sub-agent run, loads skills under ADK's rules with `load_skill` /
`load_skill_resource`, applies `generate_content_config` and `eval_config.yaml` budgets (with the free
`submit_patch`/`get_status` and the `budget_warning` injection), builds the same initial prompt, and
runs the same 3-nudge protocol and patch-extraction fallback.

```bash
# validate and print the compiled tree, tools and prompts — no API calls
.venv/bin/python scripts/submission_harness.py --dry-run --task-id fastapi_15589

# exercise the whole loop with no API key (budgets, truncation, FileEditError, nudges, patch)
.venv/bin/python scripts/submission_harness.py --task-id fastapi_15589 --auto --backend mock

# Phase 2 only, on an existing patch file
.venv/bin/python scripts/submission_harness.py --task-id fastapi_15589 \
    --verify-patch results/competition_run/patches/fastapi_15589.patch

# real run with your Google key, then Phase 2 verification
.venv/bin/python scripts/submission_harness.py --task-id fastapi_15589 --auto --verify

# closest to the real scorer: actual Gemma 4 behind an OpenAI-compatible endpoint
.venv/bin/python scripts/submission_harness.py --task-id fastapi_15589 --auto \
    --backend openai --api-base http://127.0.0.1:8000/v1 --model gemma-4-31b-it-qat-w4a16-ct
```
Interactive mode (no `--auto`) gives `/auto`, `/diff`, `/status`, `/tools`, `/tree`, `/prompt`,
`/run`, `/cat`, `/verify`, `/reset`.

**Known environment issue:** the `GOOGLE_API_KEY` in `~/.zshrc` currently has an IP-address
restriction and this machine's address is blocked (HTTP 403 `API_KEY_IP_ADDRESS_BLOCKED`). Clear the
restriction at <https://aistudio.google.com/apikey> to run live Gemini tests; `--dry-run`,
`--backend mock` and `--verify-patch` need no key.

### Fidelity limits
- A pass with **gemini-2.5-flash** says little about Gemma 4: Gemini's function calling is far more
  forgiving than vLLM's `gemma4` tool parser. Use Gemini to debug prompt logic, not to predict score.
- The older `scripts/interactive_harness.py` and `scripts/run_gemini_benchmark.py` read only
  `prompts/system.md` and `tools/tool_schemas.json`; they ignore `agent.yaml`, sub-agents, skills,
  sampling and `eval_config.yaml`. That is how a broken submission scored 1.0 locally. Prefer
  `submission_harness.py`.

### Per-task test environments
Local Phase 2 used one shared venv for every repo and commit, which made 71 of 129 tasks unscorable
(their own reference patch failed). `scripts/test_envs.py` resolves each snapshot's pins and caches a
venv keyed by a hash of the requirement set. Three verifier bugs were fixed alongside it: the
`test_patch` apply return code was ignored (a failed apply looked like a pass of the *old* tests),
the generated `pytest.ini` overrode the repo's own pytest config and disabled anyio, and only
`tests/` was reverted rather than the full protected set. `PYTHONPATH` now also covers src-layout
repos, and `-W default` stops a repo's `filterwarnings = ["error"]` turning an unrelated
DeprecationWarning into a collection error.

### Offline checks
```bash
PYTHONPATH=. .venv/bin/pytest tests/ -q          # 16 tests, incl. the ADK skill + traversal guards
.venv/bin/python scripts/validate_submission.py  # model, size, !include tree
```

---

## 6. Pre-submit checklist

- [ ] `PYTHONPATH=. .venv/bin/pytest tests/ -q` passes.
- [ ] `submission_harness.py --dry-run` prints "Validation clean".
- [ ] Skills load with the real ADK loader:
      `python -c "from google.adk.skills._utils import _load_skill_from_dir as L; import glob; print([L(d).name for d in glob.glob('skills/*')])"`
- [ ] No `!include` with `..` or a leading `/` anywhere.
- [ ] No `adapters/` unless the adapter is trained and vLLM-tested.
- [ ] All agent YAMLs declare the same allowed model.
- [ ] No `{word}` braces in any instruction unless it is a real session-state key
      (`{problem_description}`, `{hints}`) — ADK templates them and a missing key errors.
- [ ] `max_output_tokens` and `thinking_budget` ≤ 32 768; no `thinking_level`.
- [ ] If you edited `prompts/{analyzer,planner,editor,verifier}.md`, regenerate the sub-agent YAMLs so
      the inlined copies match (see the generator in the §7 commit, or re-inline by hand).
- [ ] Zip built from **inside** `sample_submission/` so `agent.yaml` is at the zip root:
      `cd sample_submission && zip -r -X ../submission.zip . -x '*.DS_Store' '*/__pycache__/*'`
- [ ] `unzip -l ../submission.zip | grep -c 'agent.yaml'` finds it at top level, and
      `--submission-dir <extracted zip> --dry-run` validates clean.
- [ ] Record the change and the score in §7.

---

## 7. Submission log

| # | Date | Change | Score |
|---|---|---|---|
| 1 | ≤ 2026-11-13 | 5-agent tree, 4 snake_case skills (compile failure), `!include ../` traversal, dummy LoRAs, 31B | 0.0 |
| 2 | 2026-11-13 | kebab-case skills + `references/`; sub-agent instructions and sampling inlined; `adapters/` removed; concrete 6-step system prompt; sub-agent prompts rewritten with fixed report formats; sampling 6144/2048; budgets 70 calls / 60 min / 180 s; same 31B model and same 5-agent topology | *pending* |
