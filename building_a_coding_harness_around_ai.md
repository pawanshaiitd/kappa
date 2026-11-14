# Building a Coding Harness Around AI

## A practical engineering guide to instructions, security, memory, agents, skills, worktrees, and autonomous workflows

**Purpose:** Learn how to build a disciplined AI coding environment that
can understand a software repository, plan changes, use tools safely,
implement features, test them, review its work, and improve through
evaluation.

**Audience:** Backend engineers, software architects, and engineers
building production-grade agentic systems.

------------------------------------------------------------------------

## 1. What is a coding harness?

An LLM can generate code, but a coding harness makes it operate within a
controlled software-engineering environment.

A coding harness is the collection of infrastructure, instructions,
tools, permissions, context-management mechanisms, execution controls,
and verification workflows surrounding an AI coding agent.

Think of the components this way:

-   **Model:** The reasoning and code-generation engine.
-   **Agent:** The model operating in a loop, using tools and
    interpreting their results.
-   **Harness:** The environment that provides context, tools, rules,
    security boundaries, execution, evaluation, and lifecycle
    management.

A capable coding harness should be able to:

1.  Understand the repository and its architecture.
2.  Identify the correct module and existing conventions.
3.  Read relevant documentation and API contracts.
4.  Plan a change before modifying code.
5.  Work within explicit permissions and boundaries.
6.  Implement code using available tools.
7.  Run unit, integration, and other relevant tests.
8.  Inspect failures and make bounded corrections.
9.  Review the final diff for correctness and risk.
10. Report what changed, what was tested, and what remains unresolved.

A more capable model does not automatically produce a better harness.
Reliability comes from the combination of model capability, context
quality, tool design, security, workflow, and evaluation.

### 1.1 High-level architecture

``` text
                         USER REQUEST
                              |
                              v
                  AI AGENT / REASONING ENGINE
                              |
           +------------------+------------------+
           |                  |                  |
           v                  v                  v
      Instructions         Knowledge            Tools
      AGENTS.md            Docs                  Terminal
      Rules                Source of truth       Editor
      Policies             Memory                Git / APIs
           |                  |                  |
           +------------------+------------------+
                              |
                              v
                     EXECUTION ENVIRONMENT
                  Permissions / Sandbox / Worktree
                              |
                              v
                    IMPLEMENTATION + TESTING
                              |
                              v
                   REVIEW / VERIFICATION / REPORT
```

### 1.2 The harness as an engineering environment

A useful analogy is an engineering team:

  Coding harness component   Engineering-team equivalent
  -------------------------- ------------------------------------------
  Main agent                 Engineer or technical lead
  Subagent                   Specialist engineer
  Tool                       Engineering equipment
  AGENTS.md / CLAUDE.md      Onboarding and project handbook
  Rules                      Engineering standards and policies
  Reference docs             Approved architecture and specifications
  Memory                     Relevant project history and decisions
  Skills                     Reusable specialist procedures
  Worktree                   Separate working copy / branch
  Sandbox                    Isolated development environment
  Evaluations                Quality assurance and competency tests
  Observability              Engineering activity and audit trail

------------------------------------------------------------------------

# Part A --- General security and instructions

## 2. Permissions and settings

An AI coding agent may be able to read source files, edit code, execute
shell commands, install dependencies, call APIs, and interact with
infrastructure. It should not receive unrestricted access by default.

A permission system defines what the agent may do and when it must ask
for approval.

### 2.1 Three permission categories

  -----------------------------------------------------------------------
  Category                Meaning                 Example
  ----------------------- ----------------------- -----------------------
  Allow                   Execute automatically   Read source files or
                                                  run approved unit tests

  Ask                     Require human approval  Install a dependency or
                                                  run a migration

  Deny                    Never execute           Read production secrets
                                                  or delete protected
                                                  data
  -----------------------------------------------------------------------

An illustrative policy might look like this:

``` json
{
  "permissions": {
    "allow": [
      "Bash(go test ./...)",
      "Bash(git diff *)"
    ],
    "ask": [
      "Bash(go get *)"
    ],
    "deny": [
      "Read(./.env)",
      "Read(./secrets/**)"
    ]
  }
}
```

This is conceptual example syntax, not a universal configuration format.
Exact permission syntax depends on the coding harness and its version.

### 2.2 Three complementary security controls

**Tool permissions**

Control whether an agent is authorized to use a tool, read a file, or
execute an operation.

**Sandboxing**

Restrict the environment in which the agent can operate. For example,
the agent may be confined to a workspace and prevented from accessing
arbitrary host paths or unrestricted network resources.

**Human approval**

Require explicit approval for sensitive operations such as production
deployment, destructive database changes, protected-branch pushes, or
access to sensitive infrastructure.

These controls solve different problems. A prompt saying "do not deploy"
is not a substitute for denying deployment permissions or requiring an
approval gate.

### 2.3 Suggested risk policy

  Operation                                 Suggested policy
  ----------------------------------------- -------------------------------------
  Explore repository                        Allow
  Read approved documentation               Allow
  Run unit tests                            Allow
  Run linters and formatters                Allow
  Edit files inside task scope              Allow
  Install new dependencies                  Ask
  Modify database schema                    Ask
  Change architecture or public contracts   Ask
  Push to protected branches                Ask or deny
  Deploy to production                      Explicit approval
  Read production secrets                   Deny unless specifically authorized
  Destructive production operations         Deny by default

### 2.4 Security checklist

-   Use least-privilege permissions.
-   Keep secrets out of prompts and repository instructions.
-   Prefer short-lived credentials and scoped tokens.
-   Restrict filesystem and network access where possible.
-   Separate development, staging, and production credentials.
-   Log tool calls and sensitive operations.
-   Require approval for irreversible actions.
-   Treat repository content and external tool output as untrusted data.
-   Review scripts before executing them.
-   Do not assume a Git worktree is a security sandbox.

------------------------------------------------------------------------

## 3. AGENTS.md --- project instructions

An agent does not automatically know a company's architecture, coding
standards, service ownership, deployment rules, or testing conventions.

`AGENTS.md` is a common convention for giving coding agents persistent
project-level instructions. Some tools use different primary instruction
filenames. For example, Claude Code uses `CLAUDE.md`; support and
compatibility mechanisms vary by tool.

Think of this file as an onboarding guide for an AI engineer.

### 3.1 Example repository

``` text
redbus-platform/
|
+-- AGENTS.md
|
+-- bus-search/
|   +-- AGENTS.md
|   +-- src/
|   +-- pom.xml
|
+-- booking-service/
|   +-- AGENTS.md
|   +-- src/
|   +-- pom.xml
|
+-- shared/
|   +-- AGENTS.md
|
+-- docs/
    +-- architecture.md
    +-- api-contracts.md
```

### 3.2 Example root AGENTS.md

``` markdown
# Project Engineering Instructions

## Architecture
- Follow existing microservice boundaries.
- Do not introduce circular dependencies.
- Reuse approved shared libraries.
- Do not duplicate existing domain capabilities.

## Java
- Follow the repository's configured Java version.
- Use existing package and naming conventions.
- Prefer constructor-based dependency injection.
- Avoid unnecessary static service dependencies.

## API
- Maintain backward compatibility unless explicitly approved.
- Validate external inputs.
- Follow established response and error formats.
- Update API documentation when contracts change.

## Testing
- Add unit tests for new business logic.
- Run the relevant module tests.
- Add integration tests for important cross-service behavior.
- Do not modify tests merely to hide a real defect.

## Safety
- Never expose secrets in logs or reports.
- Do not deploy without approval.
- Do not modify production data.
- Request approval for database schema changes.
```

### 3.3 Root instructions versus module instructions

The root file should contain rules that apply to the entire repository.
A module-specific file should add local conventions and constraints.

Keep instructions concise. Do not paste the entire architecture document
into every instruction file. Link to maintained reference documents
instead.

### 3.4 Good instruction-writing practices

-   Prefer precise, testable statements.
-   Explain important exceptions.
-   Include actual build and test commands.
-   State what must not be changed.
-   Link to the authoritative source for detailed facts.
-   Remove outdated instructions.
-   Avoid contradictory instructions across directories.
-   Review generated instructions before treating them as authoritative.

------------------------------------------------------------------------

## 4. Initialization with `/init`

When an agent encounters an unfamiliar repository, it needs to discover
the project structure and working conventions.

In Claude Code, `/init` helps inspect a project and generate a
`CLAUDE.md` containing useful project-specific instructions. Other tools
may provide different initialization workflows.

A useful initialization process should investigate:

1.  Repository structure.
2.  Programming languages and frameworks.
3.  Build systems and dependency managers.
4.  Application entry points.
5.  Important services and module boundaries.
6.  Existing tests and test commands.
7.  Coding and formatting conventions.
8.  Configuration and deployment structure.
9.  Important architecture and API documents.
10. Security-sensitive areas.

Example output:

``` markdown
# Project Overview

This is a Go inventory-ranking service.

## Structure
- cmd/: Application entry points
- internal/: Business logic
- pkg/: Reusable libraries
- api/: gRPC definitions

## Build
go build ./...

## Tests
go test ./...

## Architecture
Kafka consumers populate Redis.
The ranking API retrieves scores through gRPC.
```

Initialization is discovery, not architectural approval. The agent can
misunderstand the repository, infer incorrect conventions, or miss
important dependencies. A human should review generated instructions and
compare them with the actual system.

### Example initialization prompt

``` text
Explore this repository without modifying source code.

1. Identify the architecture and main entry points.
2. Identify languages, frameworks, and build systems.
3. Identify important dependencies and service boundaries.
4. Identify test commands and existing test conventions.
5. Identify security-sensitive areas.
6. Find authoritative architecture and API documentation.

Prepare a proposed project instruction file.
For every instruction, explain the repository evidence
that supports it. Do not invent conventions.
```

------------------------------------------------------------------------

## 5. Rules --- specialized instructions

A large repository may contain Java, Go, Python, SQL, infrastructure
code, and database migrations. A single instruction file can become
unwieldy.

Rules allow specialized instructions to be organized by technology,
path, or task. Exact loading and matching behavior depends on the
harness.

Example structure:

``` text
.claude/
|
+-- rules/
    +-- java.md
    +-- golang.md
    +-- python.md
    +-- database.md
    +-- testing.md
```

Example `golang.md`:

``` markdown
# Go Engineering Rules

- Use context.Context for request cancellation.
- Avoid global mutable state.
- Wrap errors with useful context.
- Use structured logging.
- Close Kafka consumers and other resources.
- Avoid unnecessary goroutines.
- Define clear goroutine ownership and shutdown behavior.
- Add tests for concurrency-sensitive logic.
```

### AGENTS.md versus rules versus permissions

  Mechanism              Purpose
  ---------------------- ---------------------------------------
  Project instructions   Explain how to work in the repository
  Specialized rules      Apply focused engineering conventions
  Permissions            Control which actions are authorized
  Sandbox                Restrict where actions can execute

Instructions are not hard security enforcement. A rule that says "never
read secrets" must be backed by access controls where possible.

------------------------------------------------------------------------

## 6. Source of truth and reference files

A coding agent can draw information from model knowledge, instructions,
source code, tool results, and project documentation.

For project-specific facts, define which sources are authoritative.

Example:

``` text
docs/
|
+-- architecture.md
+-- service-ownership.md
+-- api-contracts.md
+-- database-schema.md
+-- coding-standards.md
+-- deployment-guide.md
```

Example architecture:

``` text
Bus Search
    |
    v
Inventory Aggregator
    |
    v
Kronos Ranking Service
    |
    v
Redis
```

If the approved architecture says the ranking service owns ranking
decisions, the agent should not independently introduce another ranking
database or duplicate ranking logic.

### 6.1 Instructions versus source of truth

-   Instructions explain how the agent should work.
-   Reference documents describe approved system behavior and design.
-   Source code shows the implementation that currently exists.
-   Live systems and authoritative APIs provide current operational
    facts.

These sources can disagree. The harness should define how conflicts are
resolved.

For example, if documentation conflicts with the current API schema, the
agent should identify the conflict rather than silently choosing one.

### 6.2 Reference-document hygiene

For important documents, maintain:

-   Owner.
-   Version or revision.
-   Last-reviewed date.
-   Scope.
-   Status: draft, approved, deprecated.
-   Links to related implementation or API definitions.

Do not treat stale documentation as reliable merely because it is in a
`docs/` directory.

------------------------------------------------------------------------

## 7. Planning versus execution

An agent that starts editing files before understanding the task can
make unnecessary changes and miss dependencies.

Separate planning from execution for multi-file, high-risk, or
architecturally significant tasks.

### 7.1 Phase 1: Planning

Example request:

> Add a new inventory-ranking feature.

The agent should:

1.  Explore relevant modules.
2.  Read architecture and API documentation.
3.  Identify affected files and services.
4.  Understand dependencies.
5.  Identify compatibility and operational risks.
6.  Propose an implementation.
7.  Define a test strategy.
8.  List unresolved questions.

Output: an implementation plan. No source modifications during this
phase.

### 7.2 Phase 2: Execution

After approval or satisfaction of the relevant policy gate:

1.  Implement the approved design.
2.  Add or update tests.
3.  Run formatting and static analysis.
4.  Run unit and integration tests.
5.  Inspect failures.
6.  Make bounded corrections.
7.  Review the final diff.
8.  Report results and remaining risks.

### 7.3 Example workflow instruction

``` markdown
For every task involving multiple files:

1. Explore the codebase first.
2. Prepare an implementation plan.
3. Identify affected modules.
4. Identify dependencies and risks.
5. Request approval for architectural or high-risk changes.
6. Execute the approved implementation.
7. Run relevant tests and checks.
8. Review the final diff.
9. Report changed files, tests, failures, and unresolved issues.
```

A robust harness should enforce important phase boundaries through
workflow state and permissions, not only through natural-language
instructions.

------------------------------------------------------------------------

## 8. Memory --- what should an agent remember?

Memory allows an agent to reuse useful information from earlier tasks.
It should not become an uncontrolled dump of every observation.

### 8.1 Types of memory

  -----------------------------------------------------------------------
  Memory type             Purpose                 Example
  ----------------------- ----------------------- -----------------------
  Working memory          Current reasoning and   Current implementation
                          task state              plan

  Session memory          Context within a        Files already
                          conversation            investigated

  Project memory          Persistent project      Approved architecture
                          knowledge               decisions

  Developer preference    Reusable preferences    Preferred formatting
  memory                                          conventions

  Episodic memory         Past experiences        A previous migration
                                                  failure and its cause
  -----------------------------------------------------------------------

### 8.2 Example memory architecture

``` text
Task execution and observations
            |
            v
Memory extraction
            |
            v
Importance and validity evaluation
            |
            v
Deduplication and conflict resolution
            |
            v
Persistent memory
       /          \
      v            v
Structured       Vector
memory           memory
      \            /
       v          v
     Retrieval during future tasks
```

### 8.3 Structured memory

Useful for facts that have a known schema:

``` json
{
  "project": "inventory-ranking",
  "memory_type": "architecture_decision",
  "key": "ranking_data_store",
  "value": "Redis sorted sets",
  "status": "approved",
  "owner": "ranking-team",
  "updated_at": "2026-11-10"
}
```

### 8.4 Vector memory

Useful for semantic retrieval of longer observations, discussions, and
historical experiences.

A vector database can help retrieve relevant memories even when the new
task uses different wording. It does not guarantee that retrieved
information is current or correct.

### 8.5 Memory lifecycle

1.  Observe an event or decision.
2.  Extract a candidate memory.
3.  Determine whether it is reusable.
4.  Verify its source and scope.
5.  Deduplicate it against existing memories.
6.  Resolve conflicts or mark uncertainty.
7.  Store it with provenance and timestamps.
8.  Retrieve it only when relevant.
9.  Revalidate time-sensitive facts before acting.

### 8.6 What should not be remembered blindly?

-   Every tool output.
-   Temporary errors without lasting relevance.
-   Unverified assumptions.
-   Secrets or credentials.
-   Sensitive user data without a legitimate need and appropriate
    authorization.
-   Outdated architectural decisions.
-   One-off implementation details that are unlikely to recur.

A useful memory is relevant, reusable, verified, scoped, and
maintainable.

------------------------------------------------------------------------

# Part B --- Building agents and transferring capabilities

## 9. Subagents --- specialized AI engineers

A single agent can perform research, implementation, testing, and
review. But large tasks can benefit from specialization.

Example team:

``` text
                 MAIN AGENT
             Engineering Coordinator
                       |
          +------------+------------+
          |            |            |
          v            v            v
      Research      Developer     Testing
       Agent         Agent         Agent
          |            |            |
          +------------+------------+
                       |
                       v
                 Review Agent
                       |
                       v
               Main Agent Integrates
```

### 9.1 Example responsibilities

**Main agent** - Understands the user request. - Decomposes work. -
Assigns tasks. - Coordinates dependencies. - Integrates results. -
Reports completion.

**Research agent** - Explores architecture. - Finds relevant code. -
Identifies dependencies. - Reports evidence and risks.

**Developer agent** - Implements the assigned change. - Follows
repository instructions. - Adds appropriate tests.

**Testing agent** - Designs and runs tests. - Investigates failures. -
Reports coverage gaps.

**Review agent** - Reviews correctness, security, performance, and
maintainability. - Reports findings with file and line references.

### 9.2 Illustrative orchestration

``` python
async def implement_feature(requirement):
    architecture = await research_agent.run(
        "Explore relevant architecture and dependencies"
    )

    plan = await planner.run(
        requirement,
        architecture
    )

    implementation = await coding_agent.run(plan)

    test_results = await testing_agent.run(
        implementation
    )

    review = await review_agent.run(
        implementation,
        test_results
    )

    return await integration_agent.run(
        implementation,
        test_results,
        review
    )
```

This is conceptual orchestration code, not a specific Claude Code API.

### 9.3 Tool versus subagent

  Tool                             Subagent
  -------------------------------- -----------------------------------
  Performs an operation            Performs a delegated task
  Often returns a direct result    Can reason through multiple steps
  Example: read a file             Example: investigate a service
  Usually has a narrow operation   Can use multiple tools

A subagent can use tools itself.

### 9.4 When to use subagents

Good use cases:

-   Independent research across multiple services.
-   Test development while implementation is underway.
-   Independent security review.
-   Separate investigations of unrelated defects.
-   Documentation review.

Poor use cases:

-   Several agents modifying the same files.
-   Delegating a trivial one-line change.
-   Creating many agents with overlapping responsibilities.
-   Running parallel tasks with unresolved dependencies.

Parallelism introduces coordination overhead, integration risk, and
additional model cost. Use it when tasks are genuinely independent.

------------------------------------------------------------------------

## 10. Worktrees --- isolated Git working directories

Multiple coding agents working in the same checkout can overwrite each
other's changes or create confusing Git state.

Git worktrees allow multiple working directories to share one repository
while checking out different branches.

### 10.1 Example

``` bash
# Main working directory
git checkout main

# Agent A gets a separate working directory and branch
git worktree add ../search-agent -b feature/search-api

# Agent B gets another working directory and branch
git worktree add ../ranking-agent -b feature/ranking
```

Result:

``` text
redbus-platform/
|
+-- main/
|
+-- search-agent/
|   +-- search implementation
|
+-- ranking-agent/
    +-- ranking implementation
```

Each agent can work, test, and commit independently.

### 10.2 Worktree lifecycle

``` bash
# List worktrees
git worktree list

# Create a worktree
git worktree add ../agent-a -b feature/agent-a

# Remove a worktree after its work is complete
git worktree remove ../agent-a

# Clean up stale worktree metadata
git worktree prune
```

Review the branch and uncommitted changes before removing a worktree.

### 10.3 Worktree versus subagent versus container

  -----------------------------------------------------------------------
  Concept                             What it isolates
  ----------------------------------- -----------------------------------
  Subagent                            Delegated reasoning and task
                                      context

  Worktree                            Git working directory and branch

  Container or VM                     Execution environment and
                                      operating-system resources
  -----------------------------------------------------------------------

A worktree is not a security sandbox. Worktrees on the same host can
still share machine resources and potentially access common files.

### 10.4 Example: large migration

Suppose 20 services must migrate to a new logging library.

1.  Discover all affected services.
2.  Divide them into independent tasks.
3.  Assign one agent per task or service group.
4.  Create a separate worktree for each independent task.
5.  Implement the migration.
6.  Run each service's tests.
7.  Review the changes.
8.  Merge in a controlled order.

Shared libraries and common contracts should be migrated in a
dependency-aware sequence rather than blindly in parallel.

------------------------------------------------------------------------

## 11. Skills --- reusable agent capabilities

A skill packages reusable expertise and procedures for a particular
task.

Examples:

-   Go code review.
-   Java gRPC service development.
-   Kafka consumer debugging.
-   Database migration review.
-   API contract validation.
-   Performance analysis.
-   Test generation.

A useful mental model:

-   Agent = Engineer.
-   Tool = Equipment.
-   Skill = Specialized expertise and procedure.

### 11.1 Example skill directory

``` text
.claude/
|
+-- skills/
    |
    +-- go-code-review/
    |   +-- SKILL.md
    |   +-- references/
    |   |   +-- go-standards.md
    |   +-- scripts/
    |       +-- run-linters.sh
    |
    +-- feature-development/
        +-- SKILL.md
```

### 11.2 Example SKILL.md

``` markdown
---
name: go-code-review
description: Review Go code for correctness,
concurrency safety, and maintainability.
---

# Go Code Review

When reviewing Go code:

1. Check error handling.
2. Check goroutine lifecycle and ownership.
3. Check context cancellation.
4. Check shared mutable state.
5. Check resource cleanup.
6. Check Kafka consumer behavior.
7. Check Redis connection handling.
8. Identify potential race conditions.

Run where appropriate:
- go test ./...
- go vet ./...

Report findings by severity:

CRITICAL
HIGH
MEDIUM
LOW

For every finding, include:
- File and line reference.
- Why it is a problem.
- A concrete remediation.
- Whether the issue is confirmed or needs investigation.

Do not modify code unless explicitly requested.
```

### 11.3 AGENTS.md versus skill

  -----------------------------------------------------------------------
  Project instructions                Skill
  ----------------------------------- -----------------------------------
  Project-wide guidance               Specialized reusable capability

  Explains how to work in the         Explains how to perform a task
  repository                          

  Example: follow Go project          Example: conduct a Go concurrency
  conventions                         review

  Usually part of project context     Invoked or loaded when relevant,
                                      depending on the tool
  -----------------------------------------------------------------------

A skill can contain scripts, references, templates, and checklists. It
does not have to be an autonomous agent.

### 11.4 Skill design checklist

A good skill should define:

-   Name and purpose.
-   When it should be used.
-   Required inputs.
-   Preconditions.
-   Step-by-step procedure.
-   Tools and scripts it may use.
-   Expected output format.
-   Failure handling.
-   Safety restrictions.
-   Evaluation criteria.

Keep skills focused. A single skill that attempts to handle every
engineering task becomes difficult to evaluate and maintain.

------------------------------------------------------------------------

## 12. Skill evaluations --- testing whether a skill works

A skill that looks impressive in one demonstration may still be
unreliable.

For example, a Go review skill might identify a missing context
cancellation but miss a goroutine leak. It might also report false
positives on correct code.

Treat skills as software artifacts that require testing.

### 12.1 Example evaluation dataset

  Test case                      Expected behavior
  ------------------------------ ---------------------------------
  Goroutine leak                 Identify lifecycle issue
  Missing context cancellation   Identify cancellation problem
  Redis connection leak          Identify missing cleanup
  Incorrect error wrapping       Identify error-handling problem
  Correct implementation         Avoid false positive

### 12.2 Evaluation pipeline

``` text
Test repository with known defects
              |
              v
Execute skill against test cases
              |
              v
Collect findings and outputs
              |
              v
Compare against expected results
              |
              v
Calculate quality metrics
              |
              v
Improve, version, and release skill
```

### 12.3 Useful metrics

**Precision**

Of all identified problems, how many are genuine?

`Precision = True Positives / (True Positives + False Positives)`

**Recall**

Of all genuine problems, how many did the skill identify?

`Recall = True Positives / (True Positives + False Negatives)`

**Task success rate**

How often does the skill complete the intended task correctly?

Additional coding-skill metrics:

-   Relevant tests passed.
-   Regression rate.
-   Unnecessary code changes.
-   Time to completion.
-   Token and tool cost.
-   Human acceptance rate.
-   Severity-weighted defect detection.

### 12.4 Evaluation best practices

-   Maintain a fixed benchmark dataset.
-   Include both defective and correct examples.
-   Test edge cases.
-   Test different repository sizes.
-   Track false positives and false negatives.
-   Compare versions against the same dataset.
-   Avoid tuning only to known examples.
-   Include human review for high-impact evaluations.

------------------------------------------------------------------------

## 13. Community skills --- reusing external expertise

Developers publish reusable skills for code review, testing, security,
documentation, API development, and infrastructure work.

Reusing external skills can save time, but third-party content must be
treated as untrusted.

Before installing a community skill:

1.  Read its instructions.
2.  Inspect every script.
3.  Check dependencies.
4.  Review network and filesystem access.
5.  Check whether it requests secrets.
6.  Test it in an isolated environment.
7.  Evaluate its output against known cases.
8.  Pin or record the version used.

A skill containing Markdown instructions can also contain executable
scripts. Treat it like a third-party software dependency, not like
harmless documentation.

------------------------------------------------------------------------

# Part C --- Goal-oriented and repeated execution

## 14. Goal --- outcome-oriented execution

Traditional request:

> Create a Java service.

Outcome-oriented request:

> Make the inventory-ranking service production-ready, with all relevant
> tests passing, no critical security findings, and backward-compatible
> APIs.

A goal defines the desired outcome rather than only an individual
action.

A goal-driven workflow should:

1.  Understand the desired outcome.
2.  Break it into milestones.
3.  Define measurable success criteria.
4.  Execute the work.
5.  Verify progress.
6.  Identify incomplete work.
7.  Continue within defined limits.
8.  Escalate blockers.

### Example goal specification

``` text
GOAL:
Migrate all ranking APIs to the new gRPC interface.

SUCCESS CRITERIA:
- Existing API contracts are preserved.
- All affected clients are migrated.
- Unit tests pass.
- Integration tests pass.
- Deprecated interface usage is removed.
- No critical compatibility issue remains.

CONSTRAINTS:
- Do not deploy.
- Do not modify production data.
- Request approval for schema changes.
- Report any unresolved dependency.
```

Goals should have measurable completion criteria. "Make it better" is
not a reliable success condition.

------------------------------------------------------------------------

## 15. Loop --- repeated execution

Some tasks require repeated attempts.

Example:

``` text
Repeat until tests pass or a stopping limit is reached:

1. Run tests.
2. Inspect failures.
3. Identify root cause.
4. Fix the issue if within scope.
5. Run relevant tests again.
6. Report unresolved failures.
```

Another example is periodic CI monitoring:

``` text
At each scheduled check:

1. Inspect CI status.
2. Identify new failures.
3. Analyze error logs.
4. Prepare a proposed fix.
5. Report unresolved failures.
```

### 15.1 Every loop needs guardrails

Define:

-   Maximum iterations.
-   Maximum execution time.
-   Maximum model/tool cost.
-   Allowed files and operations.
-   Conditions for stopping.
-   Conditions requiring human approval.
-   How unresolved failures are reported.

Do not create uncontrolled loops that can repeatedly make changes
without meaningful progress.

------------------------------------------------------------------------

## 16. Batch --- parallel execution at scale

Batch execution divides a large task into smaller independent units.

Example: migrate 30 Go services to a new logging standard.

``` text
                    MAIN AGENT
                        |
                 Analyze 30 services
                        |
                  Divide into groups
                        |
       +----------------+----------------+
       |                |                |
       v                v                v
    Agent 1          Agent 2          Agent 3
    1 to 3           4 to 6           7 to 9
       |                |                |
       v                v                v
    Worktree         Worktree         Worktree
       |                |                |
       v                v                v
    Tests            Tests            Tests
       |                |                |
       +----------------+----------------+
                        |
                        v
                Integration and review
```

Each task can have its own worktree, tests, and result report.

### 16.1 Batch execution checklist

-   Identify task boundaries.
-   Check dependencies between tasks.
-   Avoid concurrent edits to shared files.
-   Assign explicit ownership.
-   Set per-task limits.
-   Define output contracts.
-   Run independent tests.
-   Integrate changes in dependency order.
-   Review the combined result.

Batching is useful only when tasks are sufficiently independent. More
parallel agents do not necessarily mean faster or better results.

------------------------------------------------------------------------

# Part D --- Designing a complete coding harness

## 17. Proposed repository architecture

The following is a practical reference layout for a multi-language
backend engineering repository using a Claude Code-style setup.

It is a proposed design, not a mandatory directory layout. Adapt file
locations and configuration to the actual coding tool.

``` text
ai-coding-harness/
|
+-- AGENTS.md
|
+-- docs/
|   +-- architecture.md
|   +-- engineering-standards.md
|   +-- testing-strategy.md
|   +-- service-ownership.md
|   +-- security-policy.md
|
+-- .claude/
|   |
|   +-- settings.json
|   |
|   +-- rules/
|   |   +-- golang.md
|   |   +-- java.md
|   |   +-- python.md
|   |   +-- database.md
|   |   +-- testing.md
|   |
|   +-- agents/
|   |   +-- architect.md
|   |   +-- developer.md
|   |   +-- tester.md
|   |   +-- reviewer.md
|   |
|   +-- skills/
|       +-- feature-development/
|       |   +-- SKILL.md
|       +-- code-review/
|       |   +-- SKILL.md
|       +-- debugging/
|       |   +-- SKILL.md
|       +-- performance-analysis/
|           +-- SKILL.md
|
+-- evaluations/
|   +-- test-cases/
|   +-- expected-results/
|   +-- evaluation-runner/
|
+-- scripts/
|   +-- validate.sh
|   +-- run-tests.sh
|
+-- src/
    +-- ...
```

Important: Agent definition formats and skill-loading behavior are
tool-specific. A directory name alone does not guarantee that an agent
or skill will be discovered.

## 18. Complete feature-development workflow

Example request:

> Add a new bus-ranking algorithm.

The harness can follow this workflow:

### Step 1 --- Understand

The main agent reads project instructions, relevant rules, architecture
documents, and source code.

### Step 2 --- Research

A research agent investigates the current ranking implementation,
dependencies, data flow, and affected clients.

### Step 3 --- Plan

The main agent prepares an implementation plan and test strategy.

### Step 4 --- Approve

A human reviews architectural, security-sensitive, or high-risk changes.

### Step 5 --- Isolate

Create a dedicated worktree and feature branch.

### Step 6 --- Implement

The developer agent uses the feature-development skill and modifies only
the approved scope.

### Step 7 --- Validate

The tester agent runs unit, integration, regression, and relevant
static-analysis checks.

### Step 8 --- Review

The reviewer checks correctness, security, performance, and
maintainability.

### Step 9 --- Integrate

The main agent incorporates feedback, verifies the final diff, and
prepares a pull request.

### Step 10 --- Learn

Extract reusable, verified decisions or lessons into project
documentation or memory.

### 18.1 Suggested autonomy policy

  Operation                       Suggested control
  ------------------------------- -----------------------
  Explore code                    Autonomous
  Read approved docs              Autonomous
  Create plan                     Autonomous
  Modify scoped code              Autonomous
  Run unit tests                  Autonomous
  Fix bounded test failures       Autonomous
  Change public API contracts     Approval
  Change architecture             Approval
  Modify database schema          Approval
  Production deployment           Explicit approval
  Destructive production action   Prohibited by default

------------------------------------------------------------------------

## 19. Mapping coding harness concepts to a general agentic architecture

These concepts are closely related to multi-agent systems, but a coding
harness has additional requirements around real source code, Git state,
execution, and software verification.

  Coding harness       General agentic architecture
  -------------------- -----------------------------------
  Main coding agent    Coordinator agent
  Subagents            Specialized domain agents
  AGENTS.md            Project/system instructions
  Skills               Reusable agent capabilities
  Memory               Long-term and episodic memory
  Tools                MCP/API/tool integrations
  Permissions          Tool authorization policies
  Worktrees            Isolated Git execution workspaces
  Sandbox              Isolated execution environment
  Skill evaluations    Agent capability evaluations
  Planning/execution   Planner and executor
  Source of truth      Authoritative knowledge retrieval
  Observability        Agent traces and execution logs

### Important distinctions

A skill is not the same as an agent.

A memory is not the same as documentation.

A worktree is not the same as a sandbox.

A prompt instruction is not the same as a security control.

A successful tool call is not proof that the overall task succeeded.

These distinctions matter when designing reliable systems.

------------------------------------------------------------------------

# Part E --- Additional concepts beyond the original diagram

## 20. Hooks

Hooks are mechanisms for automatically running checks or actions at
specific points in an agent workflow. Their exact events and
configuration are tool-specific.

Potential uses:

-   Validate commands before execution.
-   Run formatters after code edits.
-   Check that tests were run before completion.
-   Record tool activity.
-   Block prohibited operations.
-   Notify an external observability system.

Example conceptual lifecycle:

``` text
Agent proposes tool call
          |
          v
    Pre-tool hook
          |
          v
    Permission check
          |
          v
    Tool execution
          |
          v
    Post-tool hook
          |
          v
    Record result and continue
```

Do not rely on hooks alone for security. Use authorization, sandboxing,
and infrastructure-level controls too.

## 21. MCP --- Model Context Protocol

MCP is a protocol for connecting AI applications to external tools and
data sources through standardized interfaces.

Examples of systems a coding agent might access:

-   GitHub.
-   Internal documentation.
-   Databases.
-   Issue trackers.
-   CI systems.
-   Monitoring platforms.
-   Approved internal APIs.

A conceptual flow:

``` text
Coding Agent
     |
     v
MCP Client
     |
     v
MCP Server
     |
     v
External Tool or Data Source
```

MCP does not automatically make an external tool safe. Review server
provenance, tool descriptions, authentication, permissions, and data
exposure.

## 22. Context engineering

An LLM has a finite context window. Loading every repository file, log,
conversation, and memory is neither practical nor reliable.

Context engineering determines what information should be available for
a specific task.

A useful context strategy:

1.  Load concise project instructions.
2.  Understand the task.
3.  Identify relevant modules.
4.  Retrieve relevant documentation.
5.  Inspect only necessary source files.
6.  Retrieve relevant memories.
7.  Preserve key findings and decisions.
8.  Remove irrelevant context where the platform allows it.
9.  Verify critical facts against authoritative sources.

Context quality is often more important than context quantity.

### Example

For a Kafka consumer bug, relevant context may include:

-   Consumer implementation.
-   Consumer configuration.
-   Offset commit behavior.
-   Retry and dead-letter queue handling.
-   Relevant tests.
-   Recent error logs.

Loading the entire repository may waste context and make important
details harder to find.

## 23. Observability and tracing

A production coding harness should provide visibility into what the
agent did and why.

Useful information includes:

-   Task ID.
-   Agent and subagent identity.
-   Model and configuration.
-   Tool calls and results.
-   Files read and modified.
-   Permission decisions.
-   Test execution results.
-   Token and cost estimates.
-   Retry count.
-   Execution duration.
-   Human approvals.
-   Final Git diff or commit reference.

Example event structure:

``` json
{
  "task_id": "task-123",
  "agent": "developer",
  "event": "tool_execution",
  "tool": "run_tests",
  "target": "go test ./...",
  "status": "success",
  "duration_ms": 4200,
  "timestamp": "2026-11-10T08:00:00Z"
}
```

Avoid logging secrets, credentials, or unnecessary personal data. Audit
records should have appropriate access controls and retention policies.

## 24. Whole-harness evaluation

Evaluating an individual skill is not enough. The complete harness also
needs evaluation.

A useful benchmark suite should measure:

  Dimension           Example metric
  ------------------- ------------------------------------
  Task correctness    Acceptance-test pass rate
  Code quality        Review findings
  Reliability         Successful completion rate
  Regression safety   Existing tests remain green
  Security            Policy violations
  Efficiency          Time and cost per task
  Autonomy            Human interventions per task
  Context quality     Relevant information retrieved
  Maintainability     Unnecessary changes and complexity

Use representative tasks, fixed test repositories, reproducible
environments, and human review for important changes.

------------------------------------------------------------------------

# Part F --- Hands-on learning plan

## 25. Seven-stage practical curriculum

The best way to learn this subject is to build a working harness
alongside the theory.

  Stage   Focus                              Practical outcome
  ------- ---------------------------------- ------------------------------------
  1       Instructions, rules, permissions   Safe coding agent
  2       Source of truth and memory         Agent understands repository
  3       Planning and execution             Controlled feature implementation
  4       Subagents and worktrees            Parallel engineering
  5       Skills and evaluations             Reusable engineering capabilities
  6       Goal, loop, and batch              Bounded autonomous execution
  7       Complete harness                   Integrated engineering environment

### Stage 1 --- Build the foundation

Create a small Go microservice repository.

Ask the coding agent to explore the repository without modifying source
code.

``` text
Explore this repository.

1. Identify the architecture.
2. Identify application entry points.
3. Identify dependencies.
4. Identify existing coding conventions.
5. Identify how tests are executed.
6. Identify security-sensitive areas.

Do not modify source code.

Prepare a proposed AGENTS.md.
Explain why each instruction is necessary.
```

Review the generated instructions manually.

### Stage 2 --- Add project knowledge

Create:

-   Architecture documentation.
-   API contracts.
-   Database schema documentation.
-   Service ownership.
-   Engineering standards.

Teach the agent to cite the source file or implementation that supports
important project claims.

### Stage 3 --- Add planning and execution

Implement a workflow that separates:

-   Repository exploration.
-   Implementation plan.
-   Approval gate.
-   Code modification.
-   Testing.
-   Final review.

### Stage 4 --- Add subagents and worktrees

Create specialist roles for research, implementation, testing, and
review.

Use separate worktrees for independent changes.

### Stage 5 --- Build skills and evaluations

Start with three skills:

1.  Go code review.
2.  Kafka debugging.
3.  Feature development.

Create test cases with known defects and expected outcomes.

### Stage 6 --- Add autonomous workflows

Implement bounded goal-oriented tasks, repeated test-and-fix loops, and
batch processing.

Set explicit limits and escalation conditions.

### Stage 7 --- Integrate and observe

Add hooks, MCP integrations where useful, tracing, cost tracking, and
whole-harness evaluations.

Run representative engineering tasks from start to finish.

------------------------------------------------------------------------

# Part G --- Recommended engineering principles

## 26. Ten principles for a reliable coding harness

1.  **Least privilege:** Give agents only the access they need.
2.  **Evidence before assumptions:** Verify project facts against source
    code and approved documentation.
3.  **Plan before large changes:** Separate understanding from
    execution.
4.  **Bounded autonomy:** Define limits, stopping conditions, and
    escalation rules.
5.  **Isolation:** Use worktrees and sandboxes appropriately.
6.  **Specialization:** Delegate independent work to focused agents.
7.  **Reusable capabilities:** Package repeatable workflows as skills.
8.  **Evaluation:** Measure task success, regression rate, false
    positives, and cost.
9.  **Observability:** Record meaningful agent activity without exposing
    secrets.
10. **Human accountability:** Keep humans responsible for high-impact
    decisions and production changes.

## 27. Glossary

  -----------------------------------------------------------------------
  Term                                Short explanation
  ----------------------------------- -----------------------------------
  Coding harness                      Environment and controls
                                      surrounding an AI coding agent

  LLM                                 Language model used for reasoning
                                      and generation

  Agent                               Model-driven system that uses tools
                                      to perform tasks

  AGENTS.md                           Common project instruction file

  CLAUDE.md                           Claude Code project instruction
                                      file

  `/init`                             Claude Code project initialization
                                      command

  Rule                                Specialized engineering instruction

  Permission                          Authorization for an operation

  Sandbox                             Restricted execution environment

  Source of truth                     Authoritative source for a project
                                      fact

  Memory                              Persisted information used in later
                                      tasks

  Subagent                            Agent delegated a specialized task

  Worktree                            Separate Git working directory

  Skill                               Reusable capability or workflow

  Skill evaluation                    Test of a skill's effectiveness

  Goal                                Desired outcome with success
                                      criteria

  Loop                                Repeated bounded execution

  Batch                               Processing independent tasks in
                                      groups

  Hook                                Workflow-triggered action or check

  MCP                                 Protocol for connecting AI
                                      applications to tools and data

  Context engineering                 Selecting and managing information
                                      supplied to the model

  Observability                       Visibility into execution, tools,
                                      decisions, and outcomes
  -----------------------------------------------------------------------

------------------------------------------------------------------------

# Part H --- Further reading

## 28. Official documentation

These resources are useful for studying a real coding-agent
implementation. Features and configuration can change, so consult the
current documentation when implementing a particular version.

-   Claude Code documentation: https://code.claude.com/docs
-   Claude Code settings: https://code.claude.com/docs/en/settings
-   Claude Code memory and instructions:
    https://code.claude.com/docs/en/memory
-   Claude Code subagents: https://code.claude.com/docs/en/sub-agents
-   Claude Code skills: https://code.claude.com/docs/en/skills
-   Claude Code common workflows:
    https://code.claude.com/docs/en/common-workflows
-   Git worktrees: https://git-scm.com/docs/git-worktree
-   Model Context Protocol: https://modelcontextprotocol.io/

------------------------------------------------------------------------

# Final perspective

A coding harness is not simply a collection of prompts or a wrapper
around an LLM.

It is an engineering system that combines:

-   Instructions and project knowledge.
-   Tool access and security.
-   Context and memory.
-   Planning and execution.
-   Specialized agents and reusable skills.
-   Isolated development environments.
-   Automated testing and evaluation.
-   Observability and human approval.

The ultimate objective is not to make an AI write more code.

It is to make an AI perform software engineering work reliably, safely,
repeatably, and measurably.

**Recommended first project:** Build a small Go microservice coding
harness, start with repository discovery and project instructions, then
progressively add permissions, memory, planning, subagents, worktrees,
skills, and evaluations.
