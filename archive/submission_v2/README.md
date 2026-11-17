Running the full 31B competition model locally is impossible on a standard laptop because it requires 4× NVIDIA L4 GPUs (96 GB VRAM) and an offline vLLM cluster. 

  To solve this, we have provided two complementary ways to run benchmarks and test your newly architected harness in /Users/pawankumar.s/Documents/gemma-4-developer-agent/ using Gemini via environment variables (GEMINI_API_KEY or
  GOOGLE_API_KEY).

  ---

  Method 1: The Native Gemini Benchmark Runner (Recommended)

  We created scripts/run_gemini_benchmark.py, a native, zero-dependency benchmark runner that connects directly to Google Gemini's REST API using Python's standard library. 

  It automatically:
   1. Reads a benchmark task from tasks.jsonl (e.g. FastAPI, Rich, Requests).
   2. Unpacks the repository snapshot from snapshots/<task_id>.tgz into an isolated temporary sandbox.
   3. Loads your new system instructions (sample_submission/prompts/system.md), rules, and tool declarations (sample_submission/tools/tool_schemas.json).
   4. Drives Gemini (default: gemini-2.5-flash or gemini-1.5-pro) through the multi-turn agent loop with native function calling.
   5. Binds the 9 sandbox tools (read_file, edit_file, write_file, run_command, get_code_neighbors, search_similar_code, get_code_subgraph, get_status, submit_patch).
   6. Extracts the final binary git diff, saves the ATIF-compatible execution trace, and writes evaluation artifacts to results/gemini_benchmark/.

  1. Set Your Gemini API Key

   1 export GEMINI_API_KEY="AIzaSyYourGeminiApiKeyHere"
   2 # (or export GOOGLE_API_KEY="AIzaSyYourGeminiApiKeyHere")

  2. Verify with a Zero-Cost Dry Run
  Before calling the API, run --dry-run to verify that the sandbox unpacks, git commits baseline, and tool schemas load properly:

   1 cd /Users/pawankumar.s/Documents/gemma-4-developer-agent
   2 .venv/bin/python scripts/run_gemini_benchmark.py --dry-run --task-idx 0
  Expected output:

   1 ======================================================================
   2   BENCHMARK TASK: fastapi_15661 (fastapi/fastapi)
   3   SNAPSHOT:       fastapi_15661.tgz (304 MB)
   4   MODEL:          gemini-2.5-flash
   5 ======================================================================
   6 [DRY RUN] Sandbox extracted successfully.
   7 Loaded System Instruction: You are the Master Orchestrator and Lead Autonomous Software Engineer...
   8 Available tools (9): ['run_command', 'read_file', 'edit_file', 'write_file', 'submit_patch', 'get_status', 'search_similar_code', 'get_code_neighbors', 'get_code_subgraph']
   9 [DRY RUN COMPLETED] Harness, sandbox, and schemas are 100% verified.

  3. Run a Real Benchmark Task with Gemini
  Execute a live agent problem-solving session on any task:

   1 # Run task 0 (fastapi_15661) using Gemini 2.5 Flash
   2 .venv/bin/python scripts/run_gemini_benchmark.py --task-idx 0 --model gemini-2.5-flash
   3
   4 # Or run by task ID with higher reasoning depth
   5 .venv/bin/python scripts/run_gemini_benchmark.py --task-id fastapi_15661 --model gemini-1.5-pro --max-turns 15

  4. Inspect Results & Extracted Patches
  The script creates structured, crash-safe artifacts under results/gemini_benchmark/<instance_id>/:

   1 results/gemini_benchmark/fastapi_15661/
   2 ├── fastapi_15661.patch      # Exact unified diff produced by the agent
   3 └── result.json              # Turn count, tool breakdown, patch size, and status

  ---

  Method 2: LiteLLM Local Proxy (Emulate Competition vLLM on Port 8000)

  In the Kaggle competition environment, the harness talks to http://127.0.0.1:8000/v1 expecting an OpenAI-compatible vLLM server. You can emulate this locally using LiteLLM, routing all local 8000 port traffic to Gemini in the
  cloud.

  1. Install LiteLLM & Start Proxy

   1 cd /Users/pawankumar.s/Documents/gemma-4-developer-agent
   2 .venv/bin/pip install litellm
   3
   4 export GEMINI_API_KEY="AIzaSyYourGeminiApiKeyHere"
   5
   6 # Start the proxy server forwarding to Gemini 2.5 Flash
   7 .venv/bin/litellm --model gemini/gemini-2.5-flash --port 8000

  2. Test the Endpoint in Another Terminal
  In a second terminal window, you can run latency tests or query the local endpoint:

   1 cd /Users/pawankumar.s/Documents/gemma-4-developer-agent
   2
   3 # Run the project's latency benchmark script against the Gemini-backed proxy
   4 .venv/bin/python experiments/run_model.py \
   5   --backend vllm \
   6   --api-base http://127.0.0.1:8000/v1 \
   7   --model-id gemini/gemini-2.5-flash

  ---

  Method 3: Offline Architecture & Compliance Benchmarks

  If you want to verify that the submission is 100% compliant with the competition rules and benchmark tool execution speeds without consuming any Gemini API tokens or network calls, run:

  1. Run the Architecture Benchmark Runner

   1 cd /Users/pawankumar.s/Documents/gemma-4-developer-agent
   2 .venv/bin/python scripts/benchmark_harness.py
  Validates declarative submission rules, tests the 3-tier surgical file matcher, and benchmarks NetworkX AST call-graph indexing.

  2. Run the Full Pytest Benchmark Suite

   1 cd /Users/pawankumar.s/Documents/gemma-4-developer-agent
   2 PYTHONPATH=. .venv/bin/pytest tests/
  Runs all 12 unit and architecture benchmark tests (100% pass rate in 0.30s).

  3. Run the Competition Submission Validator

   1 cd /Users/pawankumar.s/Documents/gemma-4-developer-agent
   2 .venv/bin/python scripts/validate_submission.py
  Confirms sample_submission passes all Kaggle rules (single model gemma-4-31b-it-qat-w4a16-ct, unpacked size 0.44 MB < 3 GiB, valid !include tree).

  ---

  Summary of Options

  ┌─────────────────────────┬───────────────────────────────────────────────────────────┬─────────────────────────────────────────────────────┐
  │ Method                  │ When to Use                                               │ Command                                             │
  ├─────────────────────────┼───────────────────────────────────────────────────────────┼─────────────────────────────────────────────────────┤
  │ run_gemini_benchmark.py │ Evaluate agent solving real SWE-bench tasks with Gemini   │ python scripts/run_gemini_benchmark.py --task-idx 0 │
  │ litellm Proxy           │ Emulate local vLLM on localhost:8000 using Gemini         │ litellm --model gemini/gemini-2.5-flash --port 8000 │
  │ benchmark_harness.py    │ Instant, zero-cost test of matcher, AST graphs, & schemas │ python scripts/benchmark_harness.py                 │
  │ validate_submission.py  │ Validate competition submission package rules             │ python scripts/validate_submission.py               │
  └─────────────────────────┴───────────────────────────────────────────────────────────┴─────────────────────────────────────────────────────┘