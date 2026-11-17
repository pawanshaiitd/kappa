# Call-graph edge types

- `calls` — function invocation or method dispatch
- `imports` — module-level import dependency
- `inherits` — class inheritance relationship

# Using the result

These tools are keyed by **dotted symbol path** (`pkg.module.Class.method`), so they only work once
you already have a symbol name — find that with `grep` first.

A symbol with many incoming `calls` edges is load-bearing: prefer a backward-compatible change
(a new keyword argument with a default, or a branch inside the existing function) over a changed
signature, because every caller is a potential regression.
