"""Exact-version compiler and isolated Vyper extraction orchestration."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from evm_storage.errors import CompilerError
from evm_storage.layout import load_layout
from evm_storage.model import StorageLayout

_SOLIDITY_IMPORT_RE = re.compile(
    r"\bimport\s+(?:(?:[^;]*?\s+from\s+)?[\"'])(?P<path>[^\"']+)[\"']\s*;"
)


def extract_solidity(
    source_path: str | Path,
    *,
    solc: str = "solc",
    contract: str | None = None,
    base_path: str | Path | None = None,
) -> StorageLayout:
    """Compile Solidity through Standard JSON and normalize ``storageLayout``."""
    executable = shutil.which(solc) if "/" not in solc else solc
    if executable is None:
        raise CompilerError(f"Solidity compiler not found: {solc}")
    entry = Path(source_path).resolve()
    root = Path(base_path).resolve() if base_path is not None else entry.parent
    sources = _collect_solidity_sources(entry, root)
    standard_input = {
        "language": "Solidity",
        "sources": sources,
        "settings": {"outputSelection": {"*": {"": ["ast"], "*": ["storageLayout", "metadata"]}}},
    }
    # Every recursively discovered source is embedded in Standard JSON, so no
    # filesystem import callback or newer --base-path CLI option is required.
    command = [executable, "--standard-json"]
    result = _run(command, json.dumps(standard_input).encode(), label="solc")
    output = _json_output(result.stdout, "solc")
    errors = output.get("errors", [])
    fatal = [item for item in errors if isinstance(item, dict) and item.get("severity") == "error"]
    if fatal:
        messages = "\n".join(
            str(item.get("formattedMessage", item.get("message"))) for item in fatal
        )
        raise CompilerError(f"Solidity compilation failed:\n{messages}")
    version = _solc_version(executable)
    return load_layout(
        output,
        language="solidity",
        contract=contract,
        compiler_version=version,
        source=str(entry),
    )


def extract_vyper(
    source_path: str | Path,
    *,
    version: str,
    contract: str | None = None,
    paths: Iterable[str | Path] = (),
    uv: str = "uv",
) -> StorageLayout:
    """Extract Vyper layout inside an exact, uv-isolated compiler environment."""
    executable = shutil.which(uv)
    if executable is None:
        raise CompilerError("uv is required for exact-version Vyper extraction")
    source_file = Path(source_path).resolve()
    search_paths = tuple(Path(path).resolve() for path in paths)
    try:
        source = source_file.read_text()
    except OSError as exc:
        raise CompilerError(f"could not read {source_file}: {exc}") from exc
    worker = Path(__file__).with_name("_vyper_worker.py")
    python = _python_for_vyper(version)
    command = [
        executable,
        "run",
        "--isolated",
        "--no-project",
        "--python",
        python,
        "--with",
        f"vyper=={version}",
        "--with",
        "setuptools<81",
        "python",
        str(worker),
    ]
    payload = json.dumps({"source": source, "contract": contract}).encode()
    try:
        result = _run(command, payload, label=f"Vyper {version}", cwd=source_file.parent)
        output = _json_output(result.stdout, f"Vyper {version} worker")
    except CompilerError as worker_error:
        try:
            parsed_version = Version(version)
        except InvalidVersion:
            raise worker_error from None
        if parsed_version < Version("0.2.16"):
            raise worker_error from None
        # The compiler's path-aware CLI can resolve project imports that the
        # single-source introspection worker cannot. Its type strings are less
        # rich, but its slot table remains authoritative.
        path_arguments = [argument for path in search_paths for argument in ("-p", str(path))]
        cli_command = [
            *command[:-2],
            "vyper",
            *path_arguments,
            "-f",
            "layout",
            str(source_file),
        ]
        result = _run(
            cli_command,
            b"",
            label=f"Vyper {version} CLI fallback",
            cwd=source_file.parent,
        )
        raw_layout = _json_output(result.stdout, f"Vyper {version} CLI")
        output = {
            "schema": "evm-storage/vyper-worker/v1",
            "compiler_version": version,
            "contract": contract,
            "layout": raw_layout,
            "extraction": {
                "method": "native-cli-fallback",
                "storage_dialect": "inline",
                "search_paths": [str(path) for path in search_paths],
                "worker_error": str(worker_error),
            },
        }
    if isinstance(output.get("error"), str):
        raise CompilerError(f"Vyper {version} extraction failed: {output['error']}")
    layout = load_layout(
        output,
        language="vyper",
        contract=contract,
        compiler_version=version,
        source=str(source_file),
    )
    metadata = dict(layout.metadata)
    if isinstance(output.get("extraction"), dict):
        metadata["extraction"] = output["extraction"]
    return StorageLayout(
        language=layout.language,
        compiler_version=layout.compiler_version,
        contract=layout.contract,
        variables=layout.variables,
        types=layout.types,
        hash_order=layout.hash_order,
        storage_dialect=layout.storage_dialect,
        source=layout.source,
        metadata=metadata,
    )


def _python_for_vyper(version: str) -> str:
    try:
        parsed = Version(version)
    except InvalidVersion as exc:
        raise CompilerError(f"invalid Vyper version: {version}") from exc
    if parsed < Version("0.3.0"):
        if parsed < Version("0.1.0b17"):
            raise CompilerError("legacy Vyper extraction currently starts at 0.1.0b17")
        return "3.8"
    if parsed < Version("0.3.2"):
        return "3.9"
    if parsed < Version("0.5.0a1"):
        return "3.10"
    return "3.11"


def _collect_solidity_sources(entry: Path, root: Path) -> dict[str, dict[str, str]]:
    pending = [entry]
    seen: set[Path] = set()
    sources: dict[str, dict[str, str]] = {}
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        try:
            content = path.read_text()
        except OSError as exc:
            raise CompilerError(f"could not read Solidity source {path}: {exc}") from exc
        try:
            name = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise CompilerError(f"source {path} is outside base path {root}") from exc
        sources[name] = {"content": content}
        for match in _SOLIDITY_IMPORT_RE.finditer(content):
            imported = match.group("path")
            if imported.startswith("."):
                candidate = (path.parent / imported).resolve()
            else:
                candidate = (root / imported).resolve()
            if candidate.exists():
                pending.append(candidate)
    return sources


def _run(
    command: list[str],
    payload: bytes,
    *,
    label: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            check=False,
            cwd=cwd,
        )
    except OSError as exc:
        raise CompilerError(f"could not start {label}: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        if result.stdout:
            try:
                output = json.loads(result.stdout)
                detail = str(output.get("error", detail))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        raise CompilerError(f"{label} exited with status {result.returncode}: {detail}")
    return result


def _json_output(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CompilerError(f"{label} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise CompilerError(f"{label} returned a non-object JSON value")
    return value


def _solc_version(executable: str) -> str | None:
    try:
        result = subprocess.run(
            [executable, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )
    except OSError:
        return None
    match = re.search(r"Version:\s*([^\s]+)", result.stdout)
    return match.group(1) if match else None
