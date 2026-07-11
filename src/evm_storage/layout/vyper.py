"""Normalize Vyper layout outputs across compiler schema epochs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from packaging.version import InvalidVersion, Version

from evm_storage.errors import LayoutError
from evm_storage.model import StorageLayout, StorageMember, StorageType, StorageVariable, parse_int

_ARRAY_RE = re.compile(r"^(?P<base>.+)\[(?P<count>[0-9]+)]$")
_BYTES_RE = re.compile(r"^(?P<kind>Bytes|String)\[(?P<count>[0-9]+)]$")
_INTEGER_RE = re.compile(r"^(?:u?int)(?P<bits>[0-9]+)$")
_FIXED_BYTES_RE = re.compile(r"^bytes(?P<count>[0-9]+)$", re.IGNORECASE)


@dataclass(slots=True)
class _TypeBuilder:
    types: dict[str, StorageType]
    compiler_version: str | None = None
    legacy_hashed: bool = False

    def parse(self, raw: str, *, n_slots: int | None = None) -> str:
        label = _repair_hashmap_serialization(raw.strip())
        type_id = f"vyper:{label}"
        if type_id in self.types:
            return type_id

        mapping = _generic(label, "HashMap")
        if mapping is not None:
            parts = _split_top_level(mapping)
            if len(parts) != 2:
                return self._opaque(type_id, label, n_slots)
            key = self.parse(parts[0])
            value = self.parse(parts[1])
            self.types[type_id] = StorageType(
                id=type_id,
                label=label,
                encoding="mapping",
                number_of_bytes=32,
                key=key,
                value=value,
            )
            return type_id

        dynamic = _generic(label, "DynArray")
        if dynamic is not None:
            parts = _split_top_level(dynamic)
            if len(parts) != 2 or not parts[1].isdigit():
                return self._opaque(type_id, label, n_slots)
            base = self.parse(parts[0])
            length = int(parts[1])
            words = (
                1 + length
                if self.legacy_hashed
                else n_slots or (1 + length * self.types[base].words)
            )
            self.types[type_id] = StorageType(
                id=type_id,
                label=label,
                encoding="vyper_dynamic_array",
                number_of_bytes=words * 32,
                base=base,
                length=length,
            )
            return type_id

        bytes_match = _BYTES_RE.fullmatch(label)
        if bytes_match:
            maximum = int(bytes_match.group("count"))
            overhead = 2 if _version_before(self.compiler_version, "0.3.0") else 1
            words = (
                1 + (maximum + 31) // 32
                if self.legacy_hashed
                else n_slots or (overhead + (maximum + 31) // 32)
            )
            self.types[type_id] = StorageType(
                id=type_id,
                label=label,
                encoding="vyper_bytes",
                number_of_bytes=words * 32,
                length=maximum,
            )
            return type_id

        array_match = _ARRAY_RE.fullmatch(label)
        if array_match and not label.startswith(("HashMap[", "DynArray[", "Bytes[", "String[")):
            base = self.parse(array_match.group("base"))
            length = int(array_match.group("count"))
            words = length if self.legacy_hashed else n_slots or length * self.types[base].words
            self.types[type_id] = StorageType(
                id=type_id,
                label=label,
                encoding="inplace",
                number_of_bytes=words * 32,
                base=base,
                length=length,
            )
            return type_id

        self.types[type_id] = StorageType(
            id=type_id,
            label=label,
            encoding="inplace" if _is_scalar(label) else "opaque",
            number_of_bytes=(n_slots or 1) * 32,
        )
        return type_id

    def add_struct(
        self,
        label: str,
        members: list[dict[str, Any]],
        *,
        n_slots: int | None = None,
    ) -> str:
        type_id = f"vyper:{label}"
        normalized: list[StorageMember] = []
        max_end = 0
        for item in members:
            child = self.parse(str(item["type"]), n_slots=_optional_int(item.get("n_slots")))
            slot = parse_int(item.get("slot", item.get("offset", 0)))
            normalized.append(StorageMember(str(item["name"]), slot, 0, child))
            width = 1 if self.legacy_hashed else self.types[child].words
            max_end = max(max_end, slot + width)
        self.types[type_id] = StorageType(
            id=type_id,
            label=label,
            encoding="inplace",
            number_of_bytes=(max_end if self.legacy_hashed else n_slots or max_end or 1) * 32,
            members=tuple(normalized),
        )
        return type_id

    def _opaque(self, type_id: str, label: str, n_slots: int | None) -> str:
        self.types[type_id] = StorageType(
            id=type_id,
            label=label,
            encoding="opaque",
            number_of_bytes=(n_slots or 1) * 32,
        )
        return type_id


def normalize_vyper_layout(
    artifact: dict[str, Any],
    *,
    contract: str | None = None,
    compiler_version: str | None = None,
    source: str | None = None,
) -> StorageLayout:
    raw, selected = _select_layout(artifact, contract)
    persistent = raw.get("storage_layout", raw)
    if not isinstance(persistent, dict):
        raise LayoutError("Vyper layout must be a mapping")

    version = compiler_version or _compiler_version(artifact)
    extraction = artifact.get("extraction")
    worker_dialect = extraction.get("storage_dialect") if isinstance(extraction, dict) else None
    legacy_hashed = worker_dialect == "legacy-hashed" or _version_before(version, "0.2.13")
    builder = _TypeBuilder({}, version, legacy_hashed)
    structured_types = artifact.get("type_definitions", raw.get("type_definitions"))
    if isinstance(structured_types, dict):
        for label, item in structured_types.items():
            if isinstance(item, dict) and isinstance(item.get("members"), list):
                builder.add_struct(
                    str(label),
                    item["members"],
                    n_slots=_optional_int(item.get("n_slots")),
                )

    leaves = _flatten_namespace(persistent)
    variables: list[StorageVariable] = []
    for name, item in leaves:
        try:
            slot = parse_int(item["slot"])
            label = str(item["type"])
            n_slots = _optional_int(item.get("n_slots"))
        except (KeyError, TypeError, ValueError) as exc:
            raise LayoutError(f"invalid Vyper layout entry {name!r}: {exc}") from exc
        repaired = _repair_hashmap_serialization(label)
        type_id = f"vyper:{repaired}"
        if type_id not in builder.types:
            type_id = builder.parse(repaired, n_slots=n_slots)
        variables.append(StorageVariable(name=name, slot=slot, offset=0, type_id=type_id))
    variables.sort(key=lambda variable: (variable.slot, variable.offset, variable.name))

    if not variables and persistent:
        raise LayoutError("Vyper layout contains no storage leaves")

    dialect = "vyper-hashed-composites" if legacy_hashed else "vyper-inline"
    return StorageLayout(
        language="vyper",
        compiler_version=version,
        contract=selected,
        variables=tuple(variables),
        types=builder.types,
        hash_order="slot-key",
        storage_dialect=dialect,
        source=source,
        metadata={
            "format": "vyper-layout",
            "hashmap_serialization_repaired": any(
                str(item.get("type")) != _repair_hashmap_serialization(str(item.get("type")))
                for _name, item in leaves
            ),
            "warnings": _version_warnings(version),
        },
    )


def _select_layout(
    artifact: dict[str, Any], contract: str | None
) -> tuple[dict[str, Any], str | None]:
    if not artifact:
        return artifact, contract
    if artifact.get("schema") == "evm-storage/vyper-worker/v1":
        layout = artifact.get("layout")
        if not isinstance(layout, dict):
            raise LayoutError("Vyper worker returned no layout")
        return layout, contract or artifact.get("contract")

    direct = artifact.get("layout")
    if isinstance(direct, dict):
        return direct, contract
    if "storage_layout" in artifact or _looks_like_flat_layout(artifact):
        return artifact, contract

    candidates: list[tuple[str, dict[str, Any]]] = []
    contracts = artifact.get("contracts")
    if isinstance(contracts, dict):
        for path, by_name in contracts.items():
            if not isinstance(by_name, dict):
                continue
            for name, output in by_name.items():
                if not isinstance(output, dict):
                    continue
                layout = output.get("layout")
                if isinstance(layout, dict):
                    candidates.append((f"{path}:{name}", layout))
    if contract is not None:
        found = [item for item in candidates if item[0] == contract]
        if not found and ":" not in contract:
            found = [item for item in candidates if item[0].rsplit(":", 1)[-1] == contract]
        if len(found) == 1:
            return found[0][1], found[0][0]
        if not found:
            raise LayoutError(f"contract {contract!r} has no Vyper layout")
        raise LayoutError(f"contract name {contract!r} is ambiguous")
    if len(candidates) == 1:
        return candidates[0][1], candidates[0][0]
    if candidates:
        raise LayoutError("multiple Vyper layouts found; pass --contract")
    raise LayoutError("no Vyper layout found")


def _flatten_namespace(
    value: dict[str, Any], prefix: tuple[str, ...] = ()
) -> list[tuple[str, dict[str, Any]]]:
    leaves: list[tuple[str, dict[str, Any]]] = []
    for name, item in value.items():
        if name in {"transient_storage_layout", "code_layout", "type_definitions"}:
            continue
        if not isinstance(name, str) or not name or not isinstance(item, dict):
            raise LayoutError("Vyper layout contains an invalid namespace node")
        is_leaf = any(key in item and not isinstance(item[key], dict) for key in ("slot", "type"))
        if is_leaf:
            if (
                "slot" not in item
                or "type" not in item
                or isinstance(item["slot"], dict)
                or isinstance(item["type"], dict)
            ):
                raise LayoutError(f"Vyper layout leaf {name!r} is incomplete")
            leaves.append((".".join((*prefix, name)), item))
        else:
            leaves.extend(_flatten_namespace(item, (*prefix, name)))
    return leaves


def _looks_like_flat_layout(value: dict[str, Any]) -> bool:
    return (
        bool(value)
        and all(isinstance(item, dict) for item in value.values())
        and any("slot" in item for item in value.values() if isinstance(item, dict))
    )


def _generic(value: str, name: str) -> str | None:
    prefix = f"{name}["
    if not value.startswith(prefix) or not value.endswith("]"):
        return None
    depth = 0
    for index, char in enumerate(value[len(name) :], start=len(name)):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0 and index != len(value) - 1:
                return None
    return value[len(prefix) : -1]


def _split_top_level(value: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(value):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return parts


def _repair_hashmap_serialization(value: str) -> str:
    """Repair Vyper 0.2.16-0.3.1's duplicated HashMap display string."""
    if not value.startswith("HashMap["):
        return value
    depth = 0
    close = None
    for index, char in enumerate(value):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                close = index
                break
    if close is None or close == len(value) - 1:
        return value
    suffix = value[close + 1 :]
    inner = value[len("HashMap") : close + 1]
    if suffix == inner:
        return value[: close + 1]
    return value


def _is_scalar(label: str) -> bool:
    return bool(
        label
        in {
            "address",
            "bool",
            "decimal",
            "bytes32",
            "int128",
            "uint256",
        }
        or _INTEGER_RE.fullmatch(label)
        or _FIXED_BYTES_RE.fullmatch(label)
        or label.startswith(("enum ", "flag ", "interface "))
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    parsed = parse_int(value)  # type: ignore[arg-type]
    if parsed == 0:
        raise ValueError("n_slots must be positive")
    return parsed


def _compiler_version(artifact: dict[str, Any]) -> str | None:
    compiler = artifact.get("compiler")
    if isinstance(compiler, str):
        return compiler.removeprefix("vyper-")
    version = artifact.get("compiler_version")
    return str(version) if version is not None else None


def _version_before(value: str | None, boundary: str) -> bool:
    if value is None:
        return False
    try:
        return Version(value.removeprefix("vyper-")) < Version(boundary)
    except InvalidVersion:
        return False


def _version_warnings(value: str | None) -> list[str]:
    if value is None:
        return []
    try:
        version = Version(value.removeprefix("vyper-"))
    except InvalidVersion:
        return []
    warnings: list[str] = []
    if version in {Version("0.2.13"), Version("0.2.14")}:
        warnings.append(
            "this yanked compiler can allocate nonreentrant locks over live user storage"
        )
    if Version("0.2.15") <= version <= Version("0.3.0"):
        warnings.append(
            "this compiler allocates repeated named nonreentrant locks per function; "
            "function-qualified lock entries preserve the actual slots"
        )
    return warnings
