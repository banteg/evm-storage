"""Normalize Solidity ``storageLayout`` compiler artifacts."""

from __future__ import annotations

import json
import re
from typing import Any

from evm_storage.errors import LayoutError
from evm_storage.model import StorageLayout, StorageMember, StorageType, StorageVariable, parse_int

_FIXED_ARRAY_RE = re.compile(r"\)(?P<length>[0-9]+)_storage(?:_ptr)?$")


def normalize_solidity_layout(
    artifact: dict[str, Any],
    *,
    contract: str | None = None,
    compiler_version: str | None = None,
    source: str | None = None,
) -> StorageLayout:
    raw, selected = _select_layout(artifact, contract)
    storage = raw.get("storage")
    types = raw.get("types")
    if isinstance(storage, list) and not storage and types is None:
        types = {}
    if not isinstance(storage, list) or not isinstance(types, dict):
        raise LayoutError("Solidity layout must contain storage[] and types{}")

    normalized_types: dict[str, StorageType] = {}
    for type_id, descriptor in types.items():
        if not isinstance(type_id, str) or not isinstance(descriptor, dict):
            raise LayoutError("Solidity type table contains a malformed entry")
        normalized_types[type_id] = _normalize_type(type_id, descriptor)

    variables: list[StorageVariable] = []
    declarations = _declaring_contracts(artifact)
    for item in storage:
        if not isinstance(item, dict):
            raise LayoutError("Solidity storage table contains a non-object entry")
        try:
            ast_id = int(item["astId"]) if item.get("astId") is not None else None
            compiler_contract = (
                str(item["contract"]) if item.get("contract") is not None else selected
            )
            variable = StorageVariable(
                name=str(item["label"]),
                slot=parse_int(item["slot"]),
                offset=int(item.get("offset", 0)),
                type_id=str(item["type"]),
                contract=declarations.get(ast_id, compiler_contract)
                if ast_id is not None
                else compiler_contract,
                ast_id=ast_id,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LayoutError(f"invalid Solidity storage entry: {exc}") from exc
        if not 0 <= variable.offset < 32:
            raise LayoutError(f"invalid byte offset for {variable.name}: {variable.offset}")
        variables.append(variable)

    missing = {variable.type_id for variable in variables} - normalized_types.keys()
    if missing:
        raise LayoutError(f"Solidity layout references missing types: {', '.join(sorted(missing))}")

    return StorageLayout(
        language="solidity",
        compiler_version=compiler_version or _compiler_version(artifact),
        contract=selected,
        variables=tuple(variables),
        types=normalized_types,
        hash_order="key-slot",
        storage_dialect="solidity",
        source=source,
        metadata={"format": "solc-storageLayout"},
    )


def _normalize_type(type_id: str, descriptor: dict[str, Any]) -> StorageType:
    label = descriptor.get("label")
    encoding = descriptor.get("encoding", "opaque")
    number = descriptor.get("numberOfBytes", descriptor.get("number_of_bytes", 32))
    if not isinstance(label, str) or not isinstance(encoding, str):
        raise LayoutError(f"type {type_id} has invalid label or encoding")
    try:
        number_of_bytes = int(number)
    except (TypeError, ValueError) as exc:
        raise LayoutError(f"type {type_id} has invalid numberOfBytes") from exc

    members: list[StorageMember] = []
    for member in descriptor.get("members", []):
        if not isinstance(member, dict):
            raise LayoutError(f"type {type_id} has a malformed member")
        try:
            members.append(
                StorageMember(
                    name=str(member["label"]),
                    slot=parse_int(member["slot"]),
                    offset=int(member.get("offset", 0)),
                    type_id=str(member["type"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LayoutError(f"type {type_id} has an invalid member: {exc}") from exc

    length: int | None = None
    base = descriptor.get("base")
    if base is not None:
        match = _FIXED_ARRAY_RE.search(type_id)
        if match:
            length = int(match.group("length"))
        else:
            label_match = re.search(r"\[(\d+)](?: storage)?$", label)
            if label_match:
                length = int(label_match.group(1))

    known_encodings = {"inplace", "mapping", "dynamic_array", "bytes"}
    return StorageType(
        id=type_id,
        label=label,
        encoding=encoding if encoding in known_encodings else "opaque",  # type: ignore[arg-type]
        number_of_bytes=number_of_bytes,
        key=str(descriptor["key"]) if descriptor.get("key") is not None else None,
        value=str(descriptor["value"]) if descriptor.get("value") is not None else None,
        base=str(base) if base is not None else None,
        length=length,
        members=tuple(members),
    )


def _select_layout(
    artifact: dict[str, Any], contract: str | None
) -> tuple[dict[str, Any], str | None]:
    if isinstance(artifact.get("storage"), list) and (
        isinstance(artifact.get("types"), dict)
        or (artifact.get("types") is None and not artifact["storage"])
    ):
        return artifact, contract
    direct = artifact.get("storageLayout")
    if isinstance(direct, dict):
        return direct, contract or _contract_hint(artifact)

    candidates: list[tuple[str, dict[str, Any]]] = []
    contracts = artifact.get("contracts")
    if isinstance(contracts, dict):
        for path, by_name in contracts.items():
            if not isinstance(by_name, dict):
                continue
            for name, output in by_name.items():
                if not isinstance(output, dict):
                    continue
                layout = output.get("storageLayout")
                if isinstance(layout, dict):
                    candidates.append((f"{path}:{name}", layout))

    if contract is not None:
        exact = [item for item in candidates if item[0] == contract]
        if not exact and ":" not in contract:
            exact = [item for item in candidates if item[0].rsplit(":", 1)[-1] == contract]
        if len(exact) == 1:
            return exact[0][1], exact[0][0]
        if not exact:
            raise LayoutError(f"contract {contract!r} has no storageLayout artifact")
        raise LayoutError(f"contract name {contract!r} is ambiguous; use path:Contract")
    if len(candidates) == 1:
        return candidates[0][1], candidates[0][0]
    if not candidates:
        raise LayoutError("no Solidity storageLayout artifact found")
    names = ", ".join(name for name, _layout in candidates)
    raise LayoutError(f"multiple Solidity layouts found; select one of: {names}")


def _contract_hint(artifact: dict[str, Any]) -> str | None:
    for key in ("contractName", "contract_name"):
        if isinstance(artifact.get(key), str):
            return str(artifact[key])
    return None


def _compiler_version(artifact: dict[str, Any]) -> str | None:
    compiler = artifact.get("compiler")
    if isinstance(compiler, dict) and isinstance(compiler.get("version"), str):
        return compiler["version"]
    if isinstance(compiler, str) and compiler.lower().startswith("solc"):
        return compiler
    metadata = artifact.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = None
    if isinstance(metadata, dict):
        compiler = metadata.get("compiler")
        if isinstance(compiler, dict) and isinstance(compiler.get("version"), str):
            return compiler["version"]
    return None


def _declaring_contracts(artifact: dict[str, Any]) -> dict[int, str]:
    declarations: dict[int, str] = {}
    sources = artifact.get("sources")
    if not isinstance(sources, dict):
        return declarations

    def walk(node: object, path: str, contract_name: str | None = None) -> None:
        if isinstance(node, dict):
            current = contract_name
            if node.get("nodeType") == "ContractDefinition" and isinstance(node.get("name"), str):
                current = node["name"]
            if (
                current is not None
                and node.get("nodeType") == "VariableDeclaration"
                and node.get("stateVariable") is True
                and isinstance(node.get("id"), int)
            ):
                declarations[node["id"]] = f"{path}:{current}"
            for child in node.values():
                walk(child, path, current)
        elif isinstance(node, list):
            for child in node:
                walk(child, path, contract_name)

    for path, output in sources.items():
        if isinstance(path, str) and isinstance(output, dict):
            walk(output.get("ast"), path)
    return declarations
