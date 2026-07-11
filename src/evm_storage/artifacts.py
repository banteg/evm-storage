"""Fetch verified compiler artifacts."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from eth_hash.auto import keccak

from evm_storage.errors import LayoutError
from evm_storage.layout import load_layout
from evm_storage.model import StorageLayout, normalize_address

DEFAULT_SOURCIFY_URL = "https://sourcify.dev/server"


def fetch_sourcify_layout(
    chain_id: int,
    address: str,
    *,
    base_url: str = DEFAULT_SOURCIFY_URL,
    timeout: float = 30.0,
    max_response_bytes: int = 64 * 1024 * 1024,
) -> StorageLayout:
    address = normalize_address(address)
    url = f"{base_url.rstrip('/')}/v2/contract/{chain_id}/{address}?" + urllib.parse.urlencode(
        {"fields": "all"}
    )
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_response_bytes + 1)
        if len(raw) > max_response_bytes:
            raise LayoutError(f"Sourcify response exceeds explicit {max_response_bytes}-byte limit")
        value = json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise LayoutError(f"no Sourcify match for {address} on chain {chain_id}") from exc
        raise LayoutError(f"Sourcify returned HTTP {exc.code} for {address}") from exc
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        http.client.HTTPException,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise LayoutError(f"could not fetch Sourcify artifact for {address}: {exc}") from exc
    if not isinstance(value, dict):
        raise LayoutError("Sourcify returned a non-object response")
    if isinstance(value.get("message"), str) and value.get("customCode") is not None:
        raise LayoutError(f"Sourcify rejected the request: {value['message']}")
    if not isinstance(value.get("storageLayout"), dict):
        if value.get("match") is None:
            raise LayoutError(f"no Sourcify match for {address} on chain {chain_id}")
        raise LayoutError(f"Sourcify match for {address} on chain {chain_id} has no storage layout")

    version = _find_compiler_version(value)
    language = _find_language(value)
    errors: list[str] = []
    for candidate in _layout_candidates(value):
        try:
            layout = load_layout(
                candidate,
                language=language,
                compiler_version=version,
                source=url,
            )
            return replace(
                layout,
                contract=layout.contract or _find_contract_name(value),
                metadata={
                    **layout.metadata,
                    "sourcify": _sourcify_provenance(value),
                },
            )
        except LayoutError as exc:
            errors.append(str(exc))
    detail = f": {'; '.join(errors[:3])}" if errors else ""
    raise LayoutError(f"Sourcify response contains no usable storage layout{detail}")


def _sourcify_provenance(value: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "matchId",
        "match",
        "creationMatch",
        "runtimeMatch",
        "verifiedAt",
        "chainId",
        "address",
    )
    provenance = {key: value[key] for key in keys if value.get(key) is not None}
    compilation = value.get("compilation")
    if isinstance(compilation, dict):
        compilation_keys = (
            "language",
            "compiler",
            "compilerVersion",
            "name",
            "fullyQualifiedName",
        )
        provenance["compilation"] = {
            key: compilation[key] for key in compilation_keys if compilation.get(key) is not None
        }
    runtime = value.get("runtimeBytecode")
    if isinstance(runtime, dict) and isinstance(runtime.get("onchainBytecode"), str):
        code = _hex_bytes(runtime["onchainBytecode"])
        if code is not None:
            provenance["runtime_code_hash"] = "0x" + keccak(code).hex()
            provenance["runtime_code_size"] = len(code)
    deployment = value.get("deployment")
    if isinstance(deployment, dict):
        deployment_keys = ("transactionHash", "blockNumber", "transactionIndex", "deployer")
        provenance["deployment"] = {
            key: deployment[key] for key in deployment_keys if deployment.get(key) is not None
        }
    return provenance


def _hex_bytes(value: str) -> bytes | None:
    text = value.removeprefix("0x")
    if len(text) % 2:
        return None
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def _find_contract_name(value: object) -> str | None:
    if isinstance(value, dict):
        compilation = value.get("compilation")
        if isinstance(compilation, dict):
            for key in ("fullyQualifiedName", "name"):
                if isinstance(compilation.get(key), str):
                    return compilation[key]
        for child in value.values():
            found = _find_contract_name(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_contract_name(child)
            if found:
                return found
    return None


def _layout_candidates(value: object) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get("storage"), list) and "types" in value:
            yield value
        if isinstance(value.get("storageLayout"), dict):
            yield value
            yield value["storageLayout"]
        if isinstance(value.get("layout"), dict):
            yield {"layout": value["layout"]}
        for child in value.values():
            yield from _layout_candidates(child)
    elif isinstance(value, list):
        for child in value:
            yield from _layout_candidates(child)


def _find_compiler_version(value: object) -> str | None:
    if isinstance(value, dict):
        compiler = value.get("compiler")
        if isinstance(compiler, dict) and isinstance(compiler.get("version"), str):
            return compiler["version"]
        for key in ("compilerVersion", "compiler_version"):
            if isinstance(value.get(key), str):
                return value[key]
        for child in value.values():
            found = _find_compiler_version(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_compiler_version(child)
            if found:
                return found
    return None


def _find_language(value: object) -> str | None:
    if isinstance(value, dict):
        language = value.get("language")
        if isinstance(language, str) and language.lower() in {"solidity", "vyper"}:
            return language.lower()
        compiler = value.get("compiler")
        if isinstance(compiler, str) and "vyper" in compiler.lower():
            return "vyper"
        for child in value.values():
            found = _find_language(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_language(child)
            if found:
                return found
    return None
