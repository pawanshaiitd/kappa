---
name: code-intelligence
description: Use the precomputed AST call graph and symbol embeddings to trace callers and callees of a symbol.
---

# Code Intelligence

## Objective
Understand who calls the code you are about to change, so a fix does not break other call sites.

## When to use
After you already know the symbol name. These tools are keyed by **dotted symbol path**
(`pkg.module.Class.method`), not by free text, so they cannot find code from a prose description — use grep for
that first.

## Procedure
1. `get_code_neighbors(node="pkg.module.Class.method", edge_type="calls")` — callers and callees. Short names
   such as `Class.method` resolve too, by suffix match.
2. `get_code_subgraph(nodes=["a.b.C.m", "a.b.D.n"])` — the induced subgraph between several symbols, to see how
   two areas connect.
3. `search_similar_code(query="method_name", k=5)` — other functions whose implementation resembles this one;
   useful for finding the parallel code path that needs the same fix.

## Edge types
- `calls` — function invocation or method dispatch
- `imports` — module-level import dependency
- `inherits` — class inheritance

## Interpreting results
If a symbol has many incoming `calls` edges, prefer a backward-compatible fix: a new keyword argument with a
default, or a branch inside the existing function, rather than a changed signature.
