"""Compiler layout ingestion."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evm_storage.errors import LayoutError
from evm_storage.model import StorageLayout

from .solidity import normalize_solidity_layout
from .vyper import normalize_vyper_layout

__all__ = ("load_layout", "load_layout_file", "normalize_solidity_layout", "normalize_vyper_layout")


def load_layout(
    value: dict[str, Any],
    *,
    language: str | None = None,
    contract: str | None = None,
    compiler_version: str | None = None,
    source: str | None = None,
) -> StorageLayout:
    """Load a normalized, Solidity, or Vyper compiler artifact."""
    if value.get("schema") == "evm-storage/layout/v1":
        try:
            return StorageLayout.from_dict(value)
        except (KeyError, TypeError, ValueError) as exc:
            raise LayoutError(f"invalid normalized layout: {exc}") from exc

    inferred = language or _infer_language(value)
    if inferred == "solidity":
        return normalize_solidity_layout(
            value,
            contract=contract,
            compiler_version=compiler_version,
            source=source,
        )
    if inferred == "vyper":
        return normalize_vyper_layout(
            value,
            contract=contract,
            compiler_version=compiler_version,
            source=source,
        )
    raise LayoutError("could not infer layout language; pass --language")


def load_layout_file(
    path: str | Path,
    *,
    language: str | None = None,
    contract: str | None = None,
    compiler_version: str | None = None,
) -> StorageLayout:
    file = Path(path)
    try:
        value = json.loads(file.read_text())
    except OSError as exc:
        raise LayoutError(f"could not read {file}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LayoutError(f"{file} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LayoutError(f"{file} must contain a JSON object")
    return load_layout(
        value,
        language=language,
        contract=contract,
        compiler_version=compiler_version,
        source=str(file),
    )


def _infer_language(value: dict[str, Any]) -> str | None:
    language = value.get("language")
    if isinstance(language, str):
        lowered = language.lower()
        if lowered in {"solidity", "vyper"}:
            return lowered
    compiler = value.get("compiler")
    if isinstance(compiler, str) and "vyper" in compiler.lower():
        return "vyper"
    if "storage" in value and "types" in value:
        return "solidity"
    if "storageLayout" in value or "contracts" in value:
        return "solidity"
    if "layout" in value or "storage_layout" in value:
        return "vyper"
    if value and all(
        isinstance(item, dict)
        and ("slot" in item or any(isinstance(v, dict) for v in item.values()))
        for item in value.values()
    ):
        return "vyper"
    return None
