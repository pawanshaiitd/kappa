# MkPro Coding Harness: Comprehensive Architecture Review & Improvement Plan (AGENT_PLAN.md)

**Document Reference:** `building_a_coding_harness_around_ai.md`  
**Current System:** MkPro Autonomous Multi-Agent Developer System (v5.0.0 / Java 21 / Google GenAI ADK)  
**Date:** November 2026  

---

## 1. Executive Summary & Architectural Alignment

A coding harness is not merely an LLM prompt wrapper; it is the **engineering environment that controls, constrains, verifies, and enhances AI agents**. According to `building_a_coding_harness_around_ai.md`, a production-grade coding harness must fulfill three core tenets:
1. **Security & Boundary Isolation:** Principle of least privilege, sandboxing, path validation, and explicit human approval gates.
2. **Context, Memory & Instruction Quality:** Authoritative project instructions, hierarchical rules, structured/vector memory, and strict separation between planning and execution.
3. **Execution Rigor & Verification:** Isolated Git working trees, reusable specialist skills, automated compilers/linters/tests, bounded correction loops, and whole-harness evaluations.

`mkpro` already possesses a remarkably rich, production-oriented Java foundation (JDK 21, MapDB, JGraphT, Google GenAI ADK, RabbitMQ/Internal queue worker, Markov routing, Trajectory Distillation, WebUI, and JLine terminal UI). However, when benchmarked against the principles laid out in the guide, there are critical architectural gaps around **isolated Git worktrees, three-tier granular permissions, hard phase gating (Planning vs Execution), and comprehensive benchmark evaluations**.

---

## 2. Inventory of Current Harness Assets & File Locations

| Harness Domain (Guide Reference) | MkPro Implementation | File / Directory Location |
| :--- | :--- | :--- |
| **Project Instructions** (Sec. 3) | Root Instructions & Agent Handbook | `AGENTS.md`, `AGENT.md`, `src/main/resources/AGENT.md` |
| **System & Agent Prompts** (Sec. 3, 5) | Sub-agent role instructions & Markdown prompts | `submission/prompts/*.md`, `teams/polyglot/prompts/*.md`, `teams/default.yaml` |
| **Subagents & Team Layout** (Sec. 9, 17) | Multi-agent YAML definitions & ADK Spec | `teams/default.yaml`, `teams/polyglot.yaml`, `submission/agent.yaml`, `submission/sub_agents/*.yaml`, `src/main/resources/teams/` |
| **Tool Execution & Registry** (Sec. 1.2, 9.3) | Declarative Tool Factory & Cached Registry | `src/main/java/com/mkpro/agents/ToolRegistry.java`, `src/main/java/com/mkpro/tools/MkProTools.java` |
| **Security: Command Policies** (Sec. 2.1) | Allowlist Prefix Checker & Regex Matcher | `src/main/java/com/mkpro/security/CommandPolicy.java`, `src/main/java/com/mkpro/security/RemoteCommandPolicy.java` |
| **Security: Path Validation** (Sec. 2.2) | Canonical path bounds verification | `src/main/java/com/mkpro/security/PathValidator.java`, `src/main/java/com/mkpro/security/RemotePathValidator.java` |
| **Human Approval & Review** (Sec. 2.2, 7) | Turn-level Diff Preview & Rollback | `src/main/java/com/mkpro/events/EditApprovalService.java`, `src/main/java/com/mkpro/events/EditProposal.java` |
| **Remote Sandboxing** (Sec. 2.2, 10.3) | Persistent SSH/SFTP Sandbox Manager | `src/main/java/com/mkpro/infra/ssh/SshSessionManager.java`, `src/main/java/com/mkpro/tools/SshTools.java` |
| **Git Operations** (Sec. 6, 10) | Single-checkout Git tools & AI commit tagging | `src/main/java/com/mkpro/graph/GitUtil.java`, `src/main/java/com/mkpro/tools/ShellTools.java` (git_tools) |
| **Git Worktrees** (Sec. 10) | **Missing in MkPro** | *Currently unmanaged; all agents operate on single local checkout or remote SSH* |
| **Memory: Key-Value & Projects** (Sec. 8.3) | MapDB Hot/Shared Split Persistent Store | `src/main/java/com/mkpro/CentralMemory.java`, `src/main/java/com/mkpro/tools/CentralMemoryTools.java` |
| **Memory: Vector Embeddings** (Sec. 8.4) | MapDB Vector Store & Embedding Service | `com.google.adk.memory.MapDBVectorStore`, `src/main/java/com/mkpro/tools/CodebaseSearchTools.java` |
| **Memory: Code Knowledge Graph** (Sec. 6) | AST symbol extraction via JavaParser & JGraphT | `src/main/java/com/mkpro/graph/JavaParserScanner.java`, `src/main/java/com/mkpro/graph/JGraphTBuilder.java` |
| **Planning vs Execution** (Sec. 7) | Goal Tracker & Architectural Planner | `src/main/java/com/mkpro/models/Goal.java`, `submission/prompts/architect.md`, `submission/prompts/goal_tracker.md` |
| **Supervisor Loop** (Sec. 15) | Stimulus injection & goal completion supervisor | `src/main/java/com/mkpro/routing/MakerLoop.java`, `src/main/java/com/mkpro/routing/MakerState.java` |
| **Routing & Transitions** (Sec. 9.2) | Markov tool prediction & Intent classification | `src/main/java/com/mkpro/routing/MarkovRouter.java`, `src/main/java/com/mkpro/routing/IntentClassifier.java` |
| **Skills Library** (Sec. 11) | Dual Python & Groovy scripts with SKILL.md | `submission/skills/*/`, `teams/polyglot/skills/*/` (7 built-in skills) |
| **Self-Healing Verification Gate** (Sec. 7.2) | Syntax compile-check post-edit (`test-compile`) | `src/main/java/com/mkpro/events/AutoVerifyGate.java` |
| **Learning & Trajectory Distillation** (Sec. 8.5) | Trajectory analysis, lesson promotion engine | `src/main/java/com/mkpro/maker/distillation/TrajectoryDistiller.java`, `LessonPromotionEngine.java` |
| **Queue & Worker Architecture** (Sec. 16) | RabbitMQ + In-Memory Queue Loopback Bridge | `src/main/java/com/mkpro/infra/mq/RabbitMqWorkerService.java`, `MqConfig.java` |
| **External Integrations: MCP** (Sec. 21) | Model Context Protocol JSON-RPC Client | `src/main/java/com/mkpro/tools/McpProtocolClient.java`, `McpServerConnectTools.java` |
| **Observability & Event Bus** (Sec. 23) | Reactive event bus, WebSocket & terminal sinks | `src/main/java/com/mkpro/events/MkProEventBus.java`, `WebSocketSink.java`, `TerminalSink.java` |

---

## 3. The 4–5 Core Crucial Components of the System

To build and operate a dependable AI coding harness around `mkpro`, architectural focus must be concentrated on **five fundamental pillars**:

```
+===================================================================================+
|                              MKPRO CODING HARNESS                                 |
+===================================================================================+
|  1. Context & Instruction Engine (AGENTS.md, Rules, Graph Memory & Compaction)    |
+-----------------------------------------------------------------------------------+
|  2. Multi-Agent Orchestration & Supervisor Loop (Coordinator, Subagents, Maker)   |
+-----------------------------------------------------------------------------------+
|  3. Execution Security, Sandboxing & Isolation (Permissions, Worktrees, SSH)      |
+-----------------------------------------------------------------------------------+
|  4. Verification, Self-Healing & Eval Pipeline (AutoVerify, Distiller, Benchmarks)|
+-----------------------------------------------------------------------------------+
|  5. Reusable Skills & Tool Integration Subsystem (SKILL.md, Scripts, MCP Engine) |
+===================================================================================+
```

---

### Component 1: Context & Instruction Architecture
- **Role:** Supplies the reasoning model with repository layout, conventions, project constraints, memory, and authoritative reference data while avoiding context bloat.
- **Current MkPro Reality:**
  - `AGENTS.md` and `AGENT.md` provide high-level onboarding, role definitions, and guidelines.
  - `CentralMemory` provides project-scoped key-value storage, sticky notes, and task lists (`@` notation).
  - `JGraphTBuilder` and `JavaParserScanner` build deep semantic AST relationship graphs of classes and methods.
  - `SmartEventFilterPlugin` and `ToolOutputPruner` compact turns and strip verbose tool stdout.
- **Maturity Score:** **7.5 / 10**
- **Critical Gaps & Improvement Scope:**
  1. *No Hierarchical Rules Engine:* Lacks a modular `.mkpro/rules/*.md` directory (e.g., `java.md`, `testing.md`, `database.md`) that loads contextually based on active files or active sub-agents.
  2. *Instruction vs. Source of Truth Confusion:* Architectural contracts (`docs/architecture.md`, `docs/api-contracts.md`) are not formally delineated from behavioral rules (`AGENTS.md`). Agents lack an automated mandate to resolve documentation vs. code divergence.
  3. *Token Budget Management:* Needs strict per-agent and per-turn context budgeting before prompting, rather than purely reactive truncation in `OpenAiBaseLM`.

---

### Component 2: Autonomous Multi-Agent Orchestration & Supervisor Loop
- **Role:** Decomposes complex user goals, delegates sub-tasks to specialist agents (Coder, Architect, Tester, SysAdmin), and ensures bounded, goal-directed completion.
- **Current MkPro Reality:**
  - `Coordinator` acts as the master conductor using ADK agent specifications.
  - Subagents are cleanly compartmentalized (`Coder`, `Tester`, `Architect`, `SysAdmin`, `UbuntuOps`, `GitAgent`, `DocWriter`, etc.).
  - `MakerLoop` acts as the persistent supervisor, observing turn outcomes, evaluating goal completion status, injecting stimulus, and triggering trajectory distillation.
  - `MarkovRouter` and `FastRouteEvaluator` provide learned tool and agent transitions.
- **Maturity Score:** **8.0 / 10**
- **Critical Gaps & Improvement Scope:**
  1. *Lack of Enforced Two-Phase Gating:* The harness does not structurally prevent code modifications during Phase 1 (Planning). Agents can emit edit tool calls before an implementation plan is formulated and approved.
  2. *Subagent Context Drift:* Subagent delegations are executed as fresh turns or child sessions where long-term architectural rationale can get lost without explicit parent-trajectory summarization.
  3. *Unbounded Loop Guardrails:* Although `consecutiveZeroToolTurns` and retry limits exist, there are no hard global constraints for maximum cumulative tokens and financial budget per goal.

---

### Component 3: Execution Security, Sandboxing & Isolation Boundary
- **Role:** Guarantees that the agent cannot execute destructive operations, leak secrets, alter unapproved files, or interfere with concurrently running tasks.
- **Current MkPro Reality:**
  - `CommandPolicy` enforces an allowlist of command prefixes (`mvn`, `git`, `python`, `ls`, etc.) and regex-based argument blocking.
  - `PathValidator` ensures file reads and writes remain within the project boundary.
  - `EditApprovalService` stages file write proposals, calculates line diffs, supports one-click rollback of active turn edits, and requires human approval in web/terminal.
  - `SshSessionManager` provides persistent remote sandboxing for Ubuntu environments.
- **Maturity Score:** **6.0 / 10**
- **Critical Gaps & Improvement Scope:**
  1. *Missing Git Worktree Isolation:* There is **zero Git Worktree support**. All local coding sub-agents operate directly in the developer's dirty working directory. If two sub-agents or batch jobs run concurrently, file state collisions occur immediately.
  2. *Binary Security Model vs. 3-Tier Policy:* The current policy is strictly binary (allow or deny). It lacks the standard **Allow / Ask / Deny** policy matrix (e.g., Allow `mvn test`, Ask `mvn deploy`, Deny `cat .env`).
  3. *Local Process Sandboxing:* Local commands are executed via `BasicProcessExecutor` directly on the host OS with host user permissions. There is no lightweight local containerization (Docker/bubblewrap/seatbelt).

---

### Component 4: Verification, Self-Healing & Evaluation Pipeline
- **Role:** Automates compilation, linting, test suites, and regression checks; performs bounded autonomous self-healing; and measures harness effectiveness over time.
- **Current MkPro Reality:**
  - `AutoVerifyGate` triggers automatically after file edits and runs language-specific syntax/compile checks (`mvn test-compile -q`, `python -m py_compile`, `go build`).
  - `HybridTrajectoryDistiller` and `LessonPromotionEngine` analyze failed trajectories and distill positive patterns into persistent memory.
  - `EditApprovalService` enables instant rollback of invalid turns.
- **Maturity Score:** **5.5 / 10**
- **Critical Gaps & Improvement Scope:**
  1. *Syntax Compilation is Not Verification:* `AutoVerifyGate` only tests if the code compiles (`test-compile`). It **does not run tests (`mvn test`)**, static analysis, or linters. Code that compiles can still fail every business logic requirement.
  2. *Unstructured Self-Healing Loop:* When `AutoVerifyGate` fails, error feedback is passed back, but there is no formalized 3-attempt bounded backoff protocol with automatic rollback to checkpoint upon exhaustion.
  3. *Absence of Whole-Harness Evaluation Benchmarks:* MkPro has unit tests for its own Java classes, but **no end-to-end benchmark suite** (e.g., SWE-bench style benchmark) that measures the harness's ability to solve real engineering defects, tracking Precision, Recall, Task Success Rate, and Token Cost.

---

### Component 5: Reusable Skills & Tool Integration Subsystem
- **Role:** Packages reusable domain procedures, scripts, checklists, and external protocol interfaces (MCP) so agents solve repetitive engineering tasks deterministically.
- **Current MkPro Reality:**
  - Built-in dual-language skill library (`submission/skills/` and `teams/polyglot/skills/`) covering `codebase_search`, `file_operations`, `git_operations`, `memory_management`, `repo_navigation`, `shell_execution`, and `ssh_remote_ops`.
  - Skills include both Python CLI scripts and in-process Groovy scripts (`ScriptEngine`).
  - Declarative `SKILL.md` documents explain usage, CLI parameters, and output contracts.
  - Basic MCP JSON-RPC protocol support via `McpProtocolClient`.
- **Maturity Score:** **7.0 / 10**
- **Critical Gaps & Improvement Scope:**
  1. *Static vs. Dynamic Skill Loading:* Skills are currently statically wired. Agents cannot dynamically discover, activate, or unload skills on demand based on context.
  2. *Skills Lack Automated Evals:* None of the skills have corresponding test cases with known defects or expected outputs to measure precision and false-positive rates.
  3. *Skill Pre/Post Hooks:* Skills do not integrate with lifecycle hooks (e.g., pre-tool parameter sanitization and post-tool linting).

---

## 4. Overall Scorecard & Scope of Improvement

| Crucial Core Component | Current Maturity | Target Maturity | Highest Impact Improvement Area |
| :--- | :---: | :---: | :--- |
| **1. Context & Instruction Engine** | 7.5 / 10 | 9.5 / 10 | Modular `.mkpro/rules/*.md` & Authoritative Docs Contract |
| **2. Orchestration & Supervisor Loop**| 8.0 / 10 | 9.5 / 10 | Hard Planning vs. Execution Gating & Token/Cost Budgets |
| **3. Security, Sandboxing & Isolation** | 6.0 / 10 | 9.0 / 10 | **Git Worktrees (`git worktree`)** & 3-Tier Policy (`Allow/Ask/Deny`) |
| **4. Verification & Self-Healing** | 5.5 / 10 | 9.0 / 10 | Full Test Runner (`mvn test`), Bounded Fix Loop & E2E Evals |
| **5. Skills & Tool Integration** | 7.0 / 10 | 9.0 / 10 | Dynamic Skill Discovery, Skill Test Harness & Lifecycle Hooks |
| **Overall Harness Score** | **6.8 / 10** | **9.2 / 10** | **Transforming from Tool Runner to Disciplined SWE Harness** |

---

## 5. Detailed Execution Plan to Improve the Harness

### Phase 1: Isolation & Security Hardening (Component 3)
1. **Implement Git Worktree Management (`WorktreeManager.java`):**
   - Automatically create an isolated working directory and branch (`git worktree add ../mkpro-agent-<id> -b feature/<task>`) before any coding agent begins execution.
   - Run tests and verifications exclusively inside the isolated worktree.
   - Clean up (`git worktree remove` / `git worktree prune`) after PR creation or failure.
2. **Upgrade `CommandPolicy` to a 3-Tier Policy Model:**
   - Configure declarative policies (`policy.yaml` / `policy.json`):
     - `ALLOW`: `mvn test-compile`, `git diff`, `git status`, `ls`, `grep`
     - `ASK`: `mvn package`, `git push`, `docker build`, dependency installations
     - `DENY`: `cat .env`, destructive `rm -rf`, production credentials
   - Wire interactive approval prompts into both Terminal UI and WebUI for `ASK` actions.

### Phase 2: Enforced Planning vs. Execution Gating (Component 2 & 1)
1. **Structural Phase Enforcement in `MakerLoop`:**
   - State 1: `PLANNING` — Only read-only tools allowed (`read_file`, `search_codebase`, `fetch_url`, `git_diff`). Any write tool call (`safe_write`, `replace_in_file`, destructive shell commands) is blocked by the harness.
   - State 2: `APPROVAL_GATE` — Human or automated policy approves the written implementation plan and test strategy.
   - State 3: `EXECUTION` — Editing tools unlocked within approved file scope.
   - State 4: `VERIFY & REVIEW` — Auto-verify gate and diff review before finalizing.
2. **Hierarchical Rules Engine (`.mkpro/rules/`):**
   - Establish `.mkpro/rules/java.md`, `.mkpro/rules/testing.md`, `.mkpro/rules/git.md`.
   - Implement rule injection in `AgentManager` based on file types detected in the active workspace.

### Phase 3: Comprehensive Verification & Self-Healing Pipeline (Component 4)
1. **Upgrade `AutoVerifyGate` to Execute Actual Test Suites:**
   - Enhance `AutoVerifyGate` to execute `mvn test -Dtest=<TargetTest>` for affected modules, not just `mvn test-compile`.
   - Add static analysis / linter checks (Checkstyle, SpotBugs, or ESLint depending on language).
2. **Implement Bounded Self-Healing Correction Loop:**
   - When test/compile fails, auto-inject structured error logs into the next turn.
   - Cap autonomous fixes at 3 attempts. If turn 3 fails, automatically trigger `EditApprovalService.revertTurnSnapshots()` and escalate to the human user with a diagnostic report.

### Phase 4: Dynamic Skills & Whole-Harness Evaluation Suite (Component 5 & 4)
1. **Dynamic Skill Activation:**
   - Provide an `activate_skill` tool to agents, enabling them to load specialized skill instructions and scripts only when relevant to the current problem (saving context tokens).
2. **Harness Benchmark Runner (`Evaluations/`):**
   - Build a reproducible benchmark harness in `src/test/java/com/mkpro/evals/` containing 20 synthetic real-world bugs.
   - Automatically score the harness across releases on:
     - **Task Pass Rate (%)**
     - **Precision & Recall of Defect Discovery**
     - **Regression Safety (Zero broken pre-existing tests)**
     - **Token Cost and Execution Latency**
