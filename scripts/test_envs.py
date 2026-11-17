#!/usr/bin/env python3
"""Per-task test environments for local Phase 2 verification.

Why this exists
---------------
`verify_agent_patch` runs pytest against a repository snapshot checked out at an arbitrary
`base_commit`, but it used the harness's own `.venv`, which pins one set of versions. A gold-patch
audit (run each task's *reference* patch through Phase 2; if the official fix fails, the machine
cannot score that task) showed only 58 of 129 tasks were scorable: fastapi snapshots pin
`starlette>=0.40.0,<0.51.0` while the shared venv has 1.6.0, and `inline-snapshot`, `dirty-equals`,
`trio` and `coverage` were absent entirely.

Building one venv per task would cost minutes and hundreds of MB each. Instead the requirement set is
resolved from the snapshot's own metadata and the venv is cached under a hash of that set, so tasks
sharing pins share an environment - in practice a handful of venvs for the whole benchmark.

Nothing here touches the agent's sandbox, which stays offline. This affects local scoring only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_ROOT = REPO_ROOT / ".venv_cache"
WHEELS_DIR = REPO_ROOT / "wheels"

# Failed builds are remembered for this process only, never written to disk. A transient index
# failure ("from versions: none") is indistinguishable from a real resolution conflict in pip's
# output, and persisting it would make one network blip permanently unscore a task.
_FAILED: Dict[str, str] = {}

# Test-requirement filenames, most specific first.
REQUIREMENT_FILES = (
    "requirements-tests.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "test-requirements.txt",
)

# Packages that are never useful inside a verification venv: linters, type checkers, docs and
# release tooling. Installing them is pure latency and they pull conflicting pins.
SKIP_PACKAGES = {
    "mypy", "black", "ruff", "flake8", "isort", "pre-commit", "asv", "twine", "build",
    "pyinstaller", "types-ujson", "types-orjson", "types-setuptools", "ruff-lsp",
    "mkdocs", "mkdocs-material", "mkdocstrings", "griffe", "pillow", "cairosvg",
}

# Repos whose runtime dependencies live in setup.py, which is not worth parsing. Keyed by the
# repo field in tasks.jsonl. Only runtime imports belong here - test tooling is read from the
# snapshot's own requirements file.
SETUP_PY_RUNTIME_DEPS: Dict[str, List[str]] = {
    "psf/requests": ["urllib3", "idna", "charset-normalizer", "certifi", "PySocks"],
}


def _normalise(name: str) -> str:
    return name.lower().replace("_", "-")


def _requirement_name(spec: str) -> str:
    """Package name from a requirement string, ignoring extras and version constraints."""
    out = spec.strip()
    for sep in ("[", ">", "<", "=", "!", "~", ";", " "):
        idx = out.find(sep)
        if idx > 0:
            out = out[:idx]
    return _normalise(out)


def _self_install_extras(line: str) -> List[str]:
    """Extras named on a self-install line: `-e .[all]` -> ["all"].

    The package itself must not be installed, but the extras it requests are real test
    dependencies (fastapi's `-e .[all]` is what supplies python-multipart), so they are expanded
    from the snapshot's own [project.optional-dependencies] instead of being discarded with it.
    """
    if "[" not in line or "]" not in line:
        return []
    inner = line[line.index("[") + 1: line.rindex("]")]
    return [e.strip() for e in inner.split(",") if e.strip()]


def _read_requirements_file(path: Path, root: Path, depth: int = 0,
                            extras: Optional[List[str]] = None) -> List[str]:
    """Read a requirements file, following `-r` includes and dropping self-installs.

    `-e .` / `.[all]` are dropped deliberately: the repository under test is supplied to pytest via
    PYTHONPATH and must NOT be pip-installed, or the installed copy would shadow the patched
    workspace and silently verify the wrong code.
    """
    if depth > 5 or not path.is_file():
        return []
    specs: List[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-e", "--editable")):
            if extras is not None:
                extras.extend(_self_install_extras(line))
            continue
        if line.startswith(("-r", "--requirement")):
            target = line.split(maxsplit=1)[-1].strip()
            specs.extend(
                _read_requirements_file((path.parent / target).resolve(), root, depth + 1, extras)
            )
            continue
        if line.startswith("-"):
            continue
        if line.startswith("."):
            if extras is not None:
                extras.extend(_self_install_extras(line))
            continue
        if _requirement_name(line) in SKIP_PACKAGES:
            continue
        specs.append(line)
    return specs


def _poetry_dep_to_spec(name: str, value: Any) -> Optional[str]:
    """Poetry dependency entry -> a bare package name.

    Caret/tilde ranges are not PEP 440, and translating them adds risk for no benefit: these are
    runtime imports whose exact version rarely decides whether a test collects. The one pin that
    genuinely matters (fastapi's starlette ceiling) comes from PEP 621 metadata, which is preserved
    verbatim by `_pyproject_runtime_deps`.
    """
    if name.lower() == "python":
        return None
    if isinstance(value, dict):
        if value.get("optional"):
            return None
        if value.get("python"):  # conditional on an interpreter we are not using
            return None
    return name


def _pyproject_runtime_deps(path: Path) -> List[str]:
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return []

    specs: List[str] = []

    # PEP 621: keep the full requirement string, pins included.
    for spec in (data.get("project") or {}).get("dependencies") or []:
        if _requirement_name(str(spec)) not in SKIP_PACKAGES:
            specs.append(str(spec))

    # Poetry.
    poetry = ((data.get("tool") or {}).get("poetry") or {})
    for name, value in (poetry.get("dependencies") or {}).items():
        spec = _poetry_dep_to_spec(name, value)
        if spec and _requirement_name(spec) not in SKIP_PACKAGES:
            specs.append(spec)

    return specs


def _pyproject_named_extras(path: Path, wanted: Iterable[str]) -> List[str]:
    """Dependencies of specific [project.optional-dependencies] groups, by name."""
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return []
    optional = (data.get("project") or {}).get("optional-dependencies") or {}
    specs: List[str] = []
    for name in wanted:
        for spec in optional.get(name, []):
            if isinstance(spec, str) and _requirement_name(spec) not in SKIP_PACKAGES:
                specs.append(spec)
    return specs


def _pyproject_test_extras(path: Path) -> List[str]:
    """PEP 735 dependency-groups / optional-dependencies groups that look test related."""
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return []
    specs: List[str] = []
    groups: Dict[str, Any] = {}
    groups.update(data.get("dependency-groups") or {})
    groups.update((data.get("project") or {}).get("optional-dependencies") or {})
    poetry_groups = (((data.get("tool") or {}).get("poetry") or {}).get("group") or {})
    for gname, gval in poetry_groups.items():
        if isinstance(gval, dict) and isinstance(gval.get("dependencies"), dict):
            groups[gname] = list(gval["dependencies"].keys())
    for gname, gspecs in groups.items():
        if gname.lower() not in ("test", "tests", "dev", "testing"):
            continue
        for spec in gspecs if isinstance(gspecs, list) else []:
            if isinstance(spec, str) and _requirement_name(spec) not in SKIP_PACKAGES:
                specs.append(spec)
    # Poetry's legacy flat table.
    legacy = (((data.get("tool") or {}).get("poetry") or {}).get("dev-dependencies") or {})
    for name, value in legacy.items():
        spec = _poetry_dep_to_spec(name, value)
        if spec and _requirement_name(spec) not in SKIP_PACKAGES:
            specs.append(spec)
    return specs


def _distribution_name(workspace: Path) -> str:
    """Name of the package under test, so it is never installed from PyPI.

    An installed copy would shadow the patched workspace on sys.path and verification would
    silently test the released code instead of the agent's fix.
    """
    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
        except (tomllib.TOMLDecodeError, OSError):
            data = {}
        for candidate in (
            (data.get("project") or {}).get("name"),
            (((data.get("tool") or {}).get("poetry") or {}).get("name")),
        ):
            if candidate:
                return _normalise(str(candidate))
    return ""


def resolve_requirements(workspace: Path, repo: str = "") -> List[str]:
    """Decide what to install so this snapshot's test suite can import and collect.

    Sources, in order: the repo's own test-requirements file (following `-r`), its pyproject
    runtime dependencies (PEP 621 pins preserved verbatim), its test dependency-group, and a
    fallback table for repos whose runtime deps live in setup.py.
    """
    workspace = Path(workspace)
    specs: List[str] = []

    requested_extras: List[str] = []
    for fname in REQUIREMENT_FILES:
        found = _read_requirements_file(workspace / fname, workspace, extras=requested_extras)
        if found:
            specs.extend(found)
            break

    pyproject = workspace / "pyproject.toml"
    specs.extend(_pyproject_runtime_deps(pyproject))
    specs.extend(_pyproject_named_extras(pyproject, requested_extras))
    specs.extend(_pyproject_test_extras(pyproject))
    specs.extend(SETUP_PY_RUNTIME_DEPS.get(repo, []))

    # Never install the package under test, under any name or extra.
    self_names = {n for n in (_distribution_name(workspace), _normalise(repo.split("/")[-1])) if n}

    # De-duplicate by package name, keeping the FIRST occurrence: the test-requirements file is
    # read first and its pins are the ones the suite was written against.
    seen: Dict[str, str] = {}
    for spec in specs:
        name = _requirement_name(spec)
        if name in self_names:
            continue
        seen.setdefault(name, spec)

    # pytest itself is only added when the repo did not pin a version of its own.
    seen.setdefault("pytest", "pytest")
    return sorted(seen.values(), key=lambda s: _requirement_name(s))


def env_key(requirements: Iterable[str]) -> str:
    payload = "\n".join(sorted(requirements))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _interpreter() -> str:
    """Prefer python3.12: it is what the shared .venv uses to import the workspace."""
    for cand in ("python3.12", sys.executable, "python3"):
        if cand and (shutil.which(cand) or Path(cand).exists()):
            return cand
    return sys.executable


def _repair_shebangs(env_dir: Path) -> None:
    """Point console scripts at this venv's own interpreter.

    pip writes an absolute shebang into every script in bin/. If the cache directory was moved or
    copied (for example after renaming the repository), `bin/pytest` points at an interpreter that
    no longer exists and every verification fails with "bad interpreter".
    """
    bin_dir = env_dir / "bin"
    want = f"#!{bin_dir / 'python'}"
    for script in bin_dir.iterdir() if bin_dir.is_dir() else []:
        if script.is_symlink() or not script.is_file():
            continue
        try:
            with script.open("rb") as fh:
                first = fh.readline()
        except OSError:
            continue
        if not first.startswith(b"#!") or b"python" not in first:
            continue
        current = first[2:].strip().split()[0].decode("utf-8", "replace")
        if current == str(bin_dir / "python") or Path(current).exists():
            continue
        body = script.read_bytes()[len(first):]
        script.write_bytes(want.encode() + b"\n" + body)


def ensure_env(
    requirements: List[str],
    cache_root: Path = CACHE_ROOT,
    offline: bool = False,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Return a cached venv satisfying `requirements`, creating it on a miss.

    Returns {"pytest": <path or None>, "python": ..., "key": ..., "created": bool,
             "ok": bool, "error": <str or None>}. A failed install is cached as a failure marker so
    the same broken requirement set is not retried for every task in a sweep.
    """
    key = env_key(requirements)
    env_dir = Path(cache_root) / key
    marker = env_dir / ".ready.json"
    pytest_bin = env_dir / "bin" / "pytest"

    if marker.is_file() and pytest_bin.is_file():
        _repair_shebangs(env_dir)
        try:
            state = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
        if state.get("ok"):
            return {"pytest": str(pytest_bin), "python": str(env_dir / "bin" / "python"),
                    "key": key, "created": False, "ok": True, "error": None}
    if key in _FAILED:
        return {"pytest": None, "python": None, "key": key, "created": False,
                "ok": False, "error": _FAILED[key]}

    if offline:
        return {"pytest": None, "python": None, "key": key, "created": False,
                "ok": False, "error": "env not cached and offline=True"}

    if verbose:
        print(f"[test-env] building {key} ({len(requirements)} requirements)")
    shutil.rmtree(env_dir, ignore_errors=True)
    env_dir.parent.mkdir(parents=True, exist_ok=True)

    def _fail(msg: str) -> Dict[str, Any]:
        _FAILED[key] = msg
        shutil.rmtree(env_dir, ignore_errors=True)
        if verbose:
            print(f"[test-env] {key} FAILED: {msg[:300]}")
        return {"pytest": None, "python": None, "key": key, "created": True,
                "ok": False, "error": msg}

    res = subprocess.run([_interpreter(), "-m", "venv", str(env_dir)],
                         capture_output=True, text=True)
    if res.returncode != 0:
        return _fail(f"venv creation failed: {res.stderr[:400]}")

    pip = env_dir / "bin" / "pip"
    cmd = [str(pip), "install", "--disable-pip-version-check", "-q", "--retries", "5"]
    if WHEELS_DIR.is_dir():
        cmd += ["--find-links", str(WHEELS_DIR)]
    cmd += requirements
    # One retry: a dropped index query surfaces as "No matching distribution found", which reads
    # exactly like a genuine conflict, so the only way to tell them apart is to try again.
    for attempt in (1, 2):
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if res.returncode == 0 and pytest_bin.is_file():
            break
        if attempt == 1 and verbose:
            print(f"[test-env] {key} install attempt 1 failed; retrying")
    if res.returncode != 0 or not pytest_bin.is_file():
        return _fail(f"pip install failed: {(res.stderr or res.stdout)[-600:]}")

    marker.write_text(json.dumps({"ok": True, "requirements": requirements}, indent=2),
                      encoding="utf-8")
    if verbose:
        print(f"[test-env] {key} ready")
    return {"pytest": str(pytest_bin), "python": str(env_dir / "bin" / "python"),
            "key": key, "created": True, "ok": True, "error": None}


def resolve_pytest(workspace: Path, repo: str = "", mode: str = "auto",
                   verbose: bool = True) -> Dict[str, Any]:
    """Pick the pytest binary for one verification run.

    mode "shared" keeps the historical behaviour (the harness's own .venv). mode "auto" resolves the
    snapshot's requirements and falls back to the shared venv if provisioning fails, so a broken
    environment degrades to the old behaviour instead of losing the task.
    """
    shared = REPO_ROOT / ".venv" / "bin" / "pytest"
    shared_str = str(shared) if shared.is_file() else "pytest"
    if mode == "shared":
        return {"pytest": shared_str, "env_key": "shared", "ok": True, "error": None}

    reqs = resolve_requirements(workspace, repo)
    info = ensure_env(reqs, verbose=verbose)
    if info["ok"]:
        return {"pytest": info["pytest"], "env_key": info["key"], "ok": True, "error": None,
                "requirements": reqs}
    return {"pytest": shared_str, "env_key": "shared-fallback", "ok": False,
            "error": info["error"], "requirements": reqs}


def main() -> None:
    import argparse
    import tarfile
    import tempfile

    ap = argparse.ArgumentParser(description="Inspect or prebuild per-task test environments.")
    ap.add_argument("--snapshot", help="path to a .tgz snapshot to resolve requirements for")
    ap.add_argument("--repo", default="", help="repo field from tasks.jsonl")
    ap.add_argument("--build", action="store_true", help="actually create the venv")
    ap.add_argument("--list", action="store_true", help="list cached environments")
    args = ap.parse_args()

    if args.list:
        if not CACHE_ROOT.is_dir():
            print("no cached environments")
            return
        for d in sorted(CACHE_ROOT.iterdir()):
            marker = d / ".ready.json"
            state = {}
            if marker.is_file():
                try:
                    state = json.loads(marker.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
            flag = "ok" if state.get("ok") else "FAILED"
            print(f"  {d.name}  {flag}  ({len(state.get('requirements', []))} reqs)")
        return

    if not args.snapshot:
        ap.error("pass --snapshot or --list")

    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(args.snapshot, "r:gz") as tar:
            members = [m for m in tar.getmembers()
                       if Path(m.name).name in
                       (*REQUIREMENT_FILES, "pyproject.toml", "requirements-docs-tests.txt")
                       and m.name.count("/") <= 1]
            tar.extractall(path=td, members=members, filter="data")
        root = Path(td)
        inner = root / "." if (root / "./pyproject.toml").exists() else root
        reqs = resolve_requirements(inner, args.repo)
        print(f"resolved {len(reqs)} requirements (key {env_key(reqs)}):")
        for r in reqs:
            print(f"  {r}")
        if args.build:
            print(json.dumps(ensure_env(reqs), indent=2))


if __name__ == "__main__":
    main()
