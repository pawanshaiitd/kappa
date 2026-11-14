"""Tests for harness tool mechanics and 3-tier matcher."""

import pytest
from scripts.harness_tools import (
    apply_replacement_3tier,
    FileEditError,
    CodeGraph,
    CodeEmbeddingIndex,
    parse_unified_diff,
    is_protected_file,
    strip_protected_files_from_patch,
)


def test_3tier_exact():
    content = "def hello():\n    print('world')\n    return 42"
    old_str = "    print('world')\n    return 42"
    new_str = "    print('gemma')\n    return 100"
    res, strategy, count = apply_replacement_3tier(content, old_str, new_str)
    assert strategy == "exact"
    assert count == 1
    assert "print('gemma')" in res
    assert "return 100" in res


def test_3tier_flexible_reindent():
    # Content has 8 spaces indentation
    content = "class A:\n    def foo():\n        x = 1\n        y = 2\n        return x + y"
    # Old string has only 4 spaces indentation
    old_str = "    x = 1\n    y = 2\n    return x + y"
    new_str = "    x = 10\n    y = 20\n    return x * y"

    res, strategy, count = apply_replacement_3tier(content, old_str, new_str)
    assert strategy == "flexible"
    assert count == 1
    # Check that new string was reindented to 8 spaces
    assert "        x = 10\n        y = 20\n        return x * y" in res


def test_3tier_regex_spacing():
    # Content has irregular spacing around delimiters
    content = "result = calculate( a=1, b=2 )"
    # Old string has different spacing
    old_str = "result=calculate(a=1,b=2)"
    new_str = "result = calculate(a=10, b=20)"

    res, strategy, count = apply_replacement_3tier(content, old_str, new_str)
    assert strategy == "regex"
    assert count == 1
    assert "result = calculate(a=10, b=20)" in res


def test_3tier_uniqueness_error():
    content = "x = 1\nx = 1\nx = 1"
    old_str = "x = 1"
    new_str = "x = 2"
    with pytest.raises(FileEditError):
        apply_replacement_3tier(content, old_str, new_str, allow_multiple=False)


def test_graph_and_embedding_queries():
    graph_path = "graphs/fastapi_ee22a4b8ca46dcce26c8c183afc4992a888d8be2.json"
    emb_path = "embeddings/fastapi_ee22a4b8ca46dcce26c8c183afc4992a888d8be2.npz"

    graph = CodeGraph(graph_path)
    res_neighbors = graph.get_code_neighbors("get_openapi")
    assert res_neighbors["status"] == "ok"
    assert res_neighbors["count"] > 0
    assert "fastapi.openapi.utils.get_openapi" in res_neighbors["node"]

    subgraph = graph.get_code_subgraph(["get_openapi", "FastAPI.openapi"])
    assert subgraph["status"] == "ok"
    assert subgraph["node_count"] >= 1

    emb_index = CodeEmbeddingIndex(emb_path)
    res_search = emb_index.search_similar_code("get_openapi", k=5)
    assert res_search["status"] == "ok"
    assert len(res_search["results"]) == 5


def test_parse_unified_diff():
    patch = """diff --git a/fastapi/applications.py b/fastapi/applications.py
--- a/fastapi/applications.py
+++ b/fastapi/applications.py
@@ -10,3 +10,4 @@
+import logging
"""
    files = parse_unified_diff(patch)
    assert len(files) == 1
    assert files[0]["new_file"] == "fastapi/applications.py"


def test_strip_protected_files_from_patch():
    patch = """diff --git a/tests/test_repro.py b/tests/test_repro.py
--- a/tests/test_repro.py
+++ b/tests/test_repro.py
@@ -0,0 +1,5 @@
+def test_something():
+    pass
diff --git a/fastapi/applications.py b/fastapi/applications.py
--- a/fastapi/applications.py
+++ b/fastapi/applications.py
@@ -10,3 +10,4 @@
+import logging
diff --git a/pytest.ini b/pytest.ini
--- a/pytest.ini
+++ b/pytest.ini
@@ -1,2 +1,3 @@
+[pytest]
"""
    clean_patch = strip_protected_files_from_patch(patch)
    assert "tests/test_repro.py" not in clean_patch
    assert "pytest.ini" not in clean_patch
    assert "fastapi/applications.py" in clean_patch
    assert "+import logging" in clean_patch

