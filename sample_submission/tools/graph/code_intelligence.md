# Tools: Code Intelligence Graph Suite

## 1. `search_similar_code(query: str, k: int = 5) -> str`
- Computes cosine similarity between precomputed 256-d symbol embeddings and query tokens.
- Returns top-k matching symbols (classes, functions, methods) with dotted paths.

## 2. `get_code_neighbors(node: str, edge_type: str | None = None, max_neighbors: int = 50) -> str`
- Queries the NetworkX AST dependency graph for incoming and outgoing caller/callee relationships.
- Edge types: `calls`, `imports`, `inherits`.

## 3. `get_code_subgraph(nodes: list[str]) -> str`
- Extracts the induced call subgraph connecting the specified list of symbol nodes.
