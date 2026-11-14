"""Harness tool implementations and emulation utilities for Gemma-4 SWE agent.

This module replicates the harness tool mechanics:
1. 3-Tier resilient file editing (exact -> flexible -> regex)
2. NetworkX symbol dependency graph queries (get_code_neighbors, get_code_subgraph)
3. Embedding cosine similarity search (search_similar_code)
4. Task patch parser for trajectory generation
"""

from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Any, List, Dict, Tuple, Optional
import numpy as np
import networkx as nx


class FileEditError(Exception):
    """Raised when file editing fails (non-unique match, not found, etc.)."""
    pass


def apply_replacement_exact(
    content: str, old_string: str, new_string: str, allow_multiple: bool = False
) -> Tuple[str, int]:
    """Tier 1: Exact character-for-character substring match."""
    norm_content = content.replace("\r\n", "\n")
    norm_old = old_string.replace("\r\n", "\n")
    norm_new = new_string.replace("\r\n", "\n")

    count = norm_content.count(norm_old)
    if count == 0:
        return content, 0
    if count > 1 and not allow_multiple:
        raise FileEditError(f"Exact match found {count} occurrences, but allow_multiple=False.")

    replaced = norm_content.replace(norm_old, norm_new) if allow_multiple else norm_content.replace(norm_old, norm_new, 1)
    return replaced, count


def apply_replacement_flexible(
    content: str, old_string: str, new_string: str, allow_multiple: bool = False
) -> Tuple[str, int]:
    """Tier 2: Line-by-line match stripping leading/trailing whitespace, preserving indentation."""
    norm_content = content.replace("\r\n", "\n")
    norm_old = old_string.replace("\r\n", "\n")
    norm_new = new_string.replace("\r\n", "\n")

    content_lines = norm_content.split("\n")
    old_lines = [line.strip() for line in norm_old.split("\n") if line.strip()]
    if not old_lines:
        return content, 0

    k = len(old_lines)
    match_indices = []

    for i in range(len(content_lines) - k + 1):
        window = [content_lines[i + j].strip() for j in range(k)]
        if window == old_lines:
            match_indices.append(i)

    if not match_indices:
        return content, 0
    if len(match_indices) > 1 and not allow_multiple:
        raise FileEditError(f"Flexible match found {len(match_indices)} occurrences, but allow_multiple=False.")

    # Apply replacement preserving original indentation of first matched line
    target_idx = match_indices[0]
    matched_first_line = content_lines[target_idx]
    matched_indent = len(matched_first_line) - len(matched_first_line.lstrip())

    old_raw_lines = [l for l in norm_old.split("\n") if l.strip()]
    old_first_indent = len(old_raw_lines[0]) - len(old_raw_lines[0].lstrip()) if old_raw_lines else 0
    indent_delta = matched_indent - old_first_indent

    new_lines = norm_new.split("\n")
    reindented_new = []
    for line in new_lines:
        if not line.strip():
            reindented_new.append("")
        else:
            cur_indent = len(line) - len(line.lstrip())
            new_indent = max(0, cur_indent + indent_delta)
            reindented_new.append((" " * new_indent) + line.lstrip())

    result_lines = content_lines[:target_idx] + reindented_new + content_lines[target_idx + k:]
    return "\n".join(result_lines), len(match_indices)


def apply_replacement_regex(
    content: str, old_string: str, new_string: str, allow_multiple: bool = False
) -> Tuple[str, int]:
    """Tier 3: Regex match tokenized around code delimiters ( ) : [ ] { } > = <."""
    norm_content = content.replace("\r\n", "\n")
    norm_old = old_string.replace("\r\n", "\n")
    norm_new = new_string.replace("\r\n", "\n")

    # Delimiters to split around
    tokens = re.split(r"([()::\[\]{}><=,\s])", norm_old)
    escaped_tokens = [re.escape(t) for t in tokens if t and not t.isspace()]
    if not escaped_tokens:
        return content, 0

    pattern_str = r"\s*".join(escaped_tokens)
    pattern = re.compile(pattern_str)
    matches = list(pattern.finditer(norm_content))

    if not matches:
        return content, 0
    if len(matches) > 1 and not allow_multiple:
        raise FileEditError(f"Regex match found {len(matches)} occurrences, but allow_multiple=False.")

    replaced = pattern.sub(lambda _: norm_new, norm_content, count=0 if allow_multiple else 1)
    return replaced, len(matches)


def apply_replacement_3tier(
    content: str, old_string: str, new_string: str, allow_multiple: bool = False
) -> Tuple[str, str, int]:
    """Executes 3-tier replacement: exact -> flexible -> regex.
    Returns (new_content, strategy_used, occurrences).
    """
    if not content:
        raise FileEditError("Target file is empty or missing.")
    if not old_string:
        raise FileEditError("old_string cannot be empty.")

    # Tier 1: Exact
    res, count = apply_replacement_exact(content, old_string, new_string, allow_multiple)
    if count > 0:
        return res, "exact", count

    # Tier 2: Flexible
    res, count = apply_replacement_flexible(content, old_string, new_string, allow_multiple)
    if count > 0:
        return res, "flexible", count

    # Tier 3: Regex
    res, count = apply_replacement_regex(content, old_string, new_string, allow_multiple)
    if count > 0:
        return res, "regex", count

    raise FileEditError("Could not find match for old_string in content across all 3 tiers.")


class CodeGraph:
    """Wraps NetworkX graph for symbol neighbor and subgraph queries."""

    def __init__(self, graph_json_path: str | Path):
        self.path = Path(graph_json_path)
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.G = nx.MultiDiGraph()
        for node in data.get("nodes", []):
            self.G.add_node(node["id"], **node)
        for edge in data.get("edges", []):
            self.G.add_edge(edge["source"], edge["target"], **edge)

    def resolve_node_name(self, query: str) -> Optional[str]:
        """Resolves symbol name using exact -> suffix -> case-insensitive -> substring."""
        if query in self.G:
            return query
        # Suffix match
        for node in self.G.nodes:
            if node.endswith("." + query) or node.endswith("/" + query):
                return node
        # Case insensitive
        query_lower = query.lower()
        for node in self.G.nodes:
            if node.lower() == query_lower or node.lower().endswith("." + query_lower):
                return node
        # Substring match
        for node in self.G.nodes:
            if query in node:
                return node
        return None

    def get_code_neighbors(
        self, node_query: str, edge_type: Optional[str] = None, max_neighbors: int = 50
    ) -> Dict[str, Any]:
        resolved = self.resolve_node_name(node_query)
        if not resolved:
            return {"status": "error", "error_type": "NodeNotFound", "error_message": f"Symbol '{node_query}' not found."}

        neighbors = set()
        for pred in self.G.predecessors(resolved):
            if edge_type is None or any(d.get("type", "").lower() == edge_type.lower() for d in self.G.get_edge_data(pred, resolved).values()):
                neighbors.add(pred)
        for succ in self.G.successors(resolved):
            if edge_type is None or any(d.get("type", "").lower() == edge_type.lower() for d in self.G.get_edge_data(resolved, succ).values()):
                neighbors.add(succ)

        sorted_n = sorted(list(neighbors))[:max_neighbors]
        return {
            "status": "ok",
            "node": resolved,
            "neighbors": sorted_n,
            "count": len(sorted_n),
            "is_truncated": len(neighbors) > max_neighbors,
        }

    def get_code_subgraph(self, nodes: List[str]) -> Dict[str, Any]:
        resolved_nodes = []
        for n in nodes:
            r = self.resolve_node_name(n)
            if r:
                resolved_nodes.append(r)

        sub = self.G.subgraph(resolved_nodes)
        edge_list = []
        for u, v, data in sub.edges(data=True):
            edge_list.append({"from": u, "to": v, "type": data.get("type", "rel")})

        return {
            "status": "ok",
            "nodes": list(sub.nodes),
            "edges": edge_list,
            "node_count": sub.number_of_nodes(),
            "edge_count": len(edge_list),
            "is_truncated": False,
        }


class CodeEmbeddingIndex:
    """Wraps pre-computed NPZ embeddings for cosine similarity code search."""

    def __init__(self, npz_path: str | Path):
        self.path = Path(npz_path)
        data = np.load(self.path)
        self.keys = list(data.files)
        matrix = np.vstack([data[k] for k in self.keys])
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        self.matrix = matrix / (norms + 1e-9)
        self.key_to_idx = {k: i for i, k in enumerate(self.keys)}

    def search_similar_code(self, query: str, k: int = 10) -> Dict[str, Any]:
        # Resolve query symbol
        idx = None
        if query in self.key_to_idx:
            idx = self.key_to_idx[query]
        else:
            for symbol, i in self.key_to_idx.items():
                if symbol.endswith("." + query) or query in symbol:
                    idx = i
                    break

        if idx is None:
            # Fallback: token overlap search on symbol names
            query_tokens = set(re.findall(r"\w+", query.lower()))
            best_score = -1
            best_idx = 0
            for symbol, i in self.key_to_idx.items():
                sym_tokens = set(re.findall(r"\w+", symbol.lower()))
                score = len(query_tokens & sym_tokens)
                if score > best_score:
                    best_score = score
                    best_idx = i
            idx = best_idx

        query_vec = self.matrix[idx]
        sims = self.matrix @ query_vec
        top_indices = np.argsort(-sims)[:k]

        results = [
            {"node_name": self.keys[i], "similarity": float(sims[i])}
            for i in top_indices
        ]
        return {
            "status": "ok",
            "query": query,
            "results": results,
            "count": len(results),
            "is_truncated": False,
        }


def parse_unified_diff(patch: str) -> List[Dict[str, Any]]:
    """Parses a git unified diff into file modifications and hunks."""
    files = []
    file_diffs = re.split(r"(?=^diff --git|\A--- )", patch, flags=re.MULTILINE)
    for fd in file_diffs:
        if not fd.strip():
            continue
        lines = fd.strip().split("\n")
        old_file, new_file = None, None
        for line in lines:
            if line.startswith("--- "):
                old_file = line[4:].strip().lstrip("a/")
            elif line.startswith("+++ "):
                new_file = line[4:].strip().lstrip("b/")
                break

        if new_file:
            files.append({
                "old_file": old_file,
                "new_file": new_file,
                "diff_text": fd,
            })
    return files


PROTECTED_CONFIG_NAMES = {
    "conftest.py",
    "pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
    ".pytest.ini",
    "sitecustomize.py",
    "usercustomize.py",
    "_swegemma_stubs.py",
}


def is_protected_file(filepath: str) -> bool:
    """Returns True if the file is a test file or test runner configuration file."""
    p = Path(filepath.strip())
    name = p.name
    if name in PROTECTED_CONFIG_NAMES or name.endswith(".pth"):
        return True
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    parts = p.parts
    if any(part in ("tests", "test", "testing") for part in parts) and name.endswith(".py"):
        return True
    return False


def strip_protected_files_from_patch(patch: str) -> str:
    """Strips diff sections targeting protected test or runner configuration files."""
    if not patch:
        return ""

    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    clean_sections = []

    for sec in sections:
        if not sec.strip():
            continue
        old_file, new_file = None, None
        for line in sec.splitlines():
            if line.startswith("--- "):
                old_file = line[4:].strip().lstrip("a/")
            elif line.startswith("+++ "):
                new_file = line[4:].strip().lstrip("b/")
                break

        target = new_file or old_file or ""
        if target and is_protected_file(target):
            continue
        clean_sections.append(sec)

    return "".join(clean_sections)

