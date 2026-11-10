---
name: code_intelligence
description: Query AST call graphs and symbol embeddings to trace root cause callers and callees.
---

# Code Intelligence Skill

## Objective
Trace symbol relationships and cross-module dependencies using precomputed AST graphs.

## Procedure
1. Identify the primary function or class associated with the issue.
2. Call `get_code_neighbors(node="pkg.module.Class.method", edge_type="calls")` to inspect callers.
3. Retrieve the minimal induced subgraph via `get_code_subgraph(nodes=[...])`.
4. Formulate the precise root-cause analysis before editing code.
