"""Command-line interface for :mod:`evm_storage`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from eth_hash.auto import keccak

from evm_storage import __version__
from evm_storage.artifacts import fetch_sourcify_layout
from evm_storage.compiler import extract_solidity, extract_vyper
from evm_storage.errors import EVMStorageError, LayoutError, RPCError
from evm_storage.layout import load_layout_file
from evm_storage.model import StorageLayout, normalize_address, parse_int, word_hex
from evm_storage.output import print_changes, print_json, warning
from evm_storage.paths import decode_location, locate
from evm_storage.proxy import resolve_proxy
from evm_storage.resolver import resolve_bundle
from evm_storage.rpc import RPCClient
from evm_storage.trace import TraceProvider

DEFAULT_RPC = "http://127.0.0.1:8545"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="evm-storage",
        description="Explain EVM storage using compiler layouts and execution traces.",
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = root.add_subparsers(dest="command", required=True)

    tx = commands.add_parser("tx", help="decode the persistent state changes from a transaction")
    tx.add_argument("tx_hash")
    tx.add_argument(
        "--layout",
        action="append",
        default=[],
        metavar="ADDRESS=ARTIFACT",
        help="compiler layout for a storage or executing-code address (repeatable)",
    )
    _rpc_options(tx)
    tx.add_argument("--no-auto-layout", action="store_true", help="do not query Sourcify")
    tx.add_argument("--sourcify-url", default="https://sourcify.dev/server")
    tx.add_argument("--json", action="store_true", dest="json_output")
    tx.set_defaults(handler=_tx)

    read = commands.add_parser("read", help="read one explicit scalar storage path")
    read.add_argument("address")
    read.add_argument("path")
    read.add_argument("--layout", required=True, metavar="ARTIFACT")
    read.add_argument("--language", choices=["solidity", "vyper"])
    read.add_argument("--contract")
    read.add_argument("--block", default="latest")
    read.add_argument("--json", action="store_true", dest="json_output")
    _rpc_options(read)
    read.set_defaults(handler=_read)

    snapshot = commands.add_parser("snapshot", help="read a list of explicit scalar paths")
    snapshot.add_argument("address")
    snapshot.add_argument("--paths", required=True, help="JSON array or newline-delimited paths")
    snapshot.add_argument("--layout", required=True, metavar="ARTIFACT")
    snapshot.add_argument("--language", choices=["solidity", "vyper"])
    snapshot.add_argument("--contract")
    snapshot.add_argument("--block", default="latest")
    _rpc_options(snapshot)
    snapshot.set_defaults(handler=_snapshot)

    diff = commands.add_parser("diff", help="compare two evm-storage snapshots")
    diff.add_argument("before")
    diff.add_argument("after")
    diff.add_argument("--json", action="store_true", dest="json_output")
    diff.set_defaults(handler=_diff)

    layout = commands.add_parser("layout", help="extract or normalize compiler layouts")
    layout_commands = layout.add_subparsers(dest="layout_command", required=True)

    normalize = layout_commands.add_parser("normalize", help="normalize a compiler artifact")
    normalize.add_argument("artifact")
    normalize.add_argument("--language", choices=["solidity", "vyper"])
    normalize.add_argument("--contract")
    normalize.add_argument("--compiler-version")
    normalize.add_argument("-o", "--output")
    normalize.set_defaults(handler=_normalize)

    extract = layout_commands.add_parser("extract", help="compile source with an exact compiler")
    extract_commands = extract.add_subparsers(dest="language", required=True)
    solidity = extract_commands.add_parser("solidity")
    solidity.add_argument("source")
    solidity.add_argument("--solc", default="solc")
    solidity.add_argument("--base-path")
    solidity.add_argument("--contract")
    solidity.add_argument("-o", "--output")
    solidity.set_defaults(handler=_extract_solidity)
    vyper = extract_commands.add_parser("vyper")
    vyper.add_argument("source")
    vyper.add_argument("--version", required=True)
    vyper.add_argument(
        "--path",
        action="append",
        default=[],
        help="Vyper import search path (repeatable)",
    )
    vyper.add_argument("--contract")
    vyper.add_argument("-o", "--output")
    vyper.set_defaults(handler=_extract_vyper)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.handler(args) or 0)
    except (EVMStorageError, ValueError) as exc:
        print(f"evm-storage: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("evm-storage: interrupted", file=sys.stderr)
        return 130


def _rpc_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--rpc-url", default=os.getenv("EVM_STORAGE_RPC_URL", DEFAULT_RPC))
    command.add_argument("--rpc-timeout", type=float, default=120.0)
    command.add_argument("--rpc-max-response-mib", type=int, default=512)


def _tx(args: argparse.Namespace) -> int:
    rpc = _rpc(args)
    bundle = TraceProvider(rpc).transaction(args.tx_hash)
    layouts = _layout_map(args.layout)
    block = bundle.transaction.get("blockNumber") or "latest"
    chain_id = None if args.no_auto_layout else rpc.chain_id()
    if chain_id is not None:
        code_addresses = {
            change.write.code_address for change in bundle.changes if change.write is not None
        }
        for address in sorted(code_addresses - layouts.keys()):
            try:
                layouts[address] = _fetch_sourcify_for_block(
                    rpc,
                    chain_id,
                    address,
                    block,
                    base_url=args.sourcify_url,
                )
            except LayoutError as exc:
                warning(str(exc))
    # A direct-storage layout is useful only when execution did not already
    # identify a code layout (for proxies, it usually has no user variables).
    storage_addresses = {
        change.address
        for change in bundle.changes
        if change.write is None or change.write.code_address not in layouts
    }
    for address in sorted(storage_addresses - layouts.keys()):
        proxy = (
            resolve_proxy(rpc, address, block=block) if layouts or chain_id is not None else None
        )
        if proxy is not None:
            implementation_layout = layouts.get(proxy.implementation)
            if implementation_layout is None and chain_id is not None:
                try:
                    implementation_layout = _fetch_sourcify_for_block(
                        rpc,
                        chain_id,
                        proxy.implementation,
                        block,
                        base_url=args.sourcify_url,
                    )
                    layouts[proxy.implementation] = implementation_layout
                except LayoutError as exc:
                    warning(str(exc))
            if implementation_layout is not None:
                layouts[address] = replace(
                    implementation_layout,
                    metadata={
                        **implementation_layout.metadata,
                        "attribution": "static-proxy",
                        "proxy_kind": proxy.kind,
                        "implementation_address": proxy.implementation,
                    },
                )
                continue
        if chain_id is not None:
            try:
                layouts[address] = _fetch_sourcify_for_block(
                    rpc,
                    chain_id,
                    address,
                    block,
                    base_url=args.sourcify_url,
                )
            except LayoutError as exc:
                warning(str(exc))
    resolved = resolve_bundle(bundle, layouts)
    if args.json_output:
        print_json(
            {
                "schema": "evm-storage/transaction/v1",
                "transaction": args.tx_hash,
                "client": bundle.client_version,
                "provenance": bundle.provenance,
                "warnings": list(bundle.warnings),
                "layouts": {
                    address: _layout_provenance(layout)
                    for address, layout in sorted(layouts.items())
                },
                "changes": [item.to_dict() for item in resolved],
            }
        )
    else:
        print_changes(resolved)
        for item in bundle.warnings:
            warning(item)
    return 0


def _read(args: argparse.Namespace) -> int:
    layout = load_layout_file(args.layout, language=args.language, contract=args.contract)
    location = locate(layout, args.path)
    address = normalize_address(args.address)
    rpc = _rpc(args)
    chain_id = rpc.chain_id()
    block, block_hash = _pin_block(rpc, args.block)
    word = rpc.storage(address, location.slot, block)
    _assert_block_unchanged(rpc, block, block_hash)
    value = decode_location(layout, location, word)
    result = {
        "address": address,
        "chain_id": chain_id,
        "block": block,
        "block_hash": block_hash,
        "path": args.path,
        "slot": word_hex(location.slot),
        "offset": location.offset,
        "type": layout.types[location.type_id].label,
        "value": value,
        "word": word_hex(word),
    }
    if args.json_output:
        print_json(result)
    else:
        print(f"{args.path} = {value}")
        print(f"slot {result['slot']}, byte offset {location.offset}")
    return 0


def _snapshot(args: argparse.Namespace) -> int:
    layout = load_layout_file(args.layout, language=args.language, contract=args.contract)
    paths = _load_paths(Path(args.paths))
    rpc = _rpc(args)
    chain_id = rpc.chain_id()
    address = normalize_address(args.address)
    block, block_hash = _pin_block(rpc, args.block)
    values: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    for path in paths:
        location = locate(layout, path)
        word = rpc.storage(address, location.slot, block)
        values[path] = decode_location(layout, location, word)
        evidence[path] = {"slot": word_hex(location.slot), "word": word_hex(word)}
    _assert_block_unchanged(rpc, block, block_hash)
    print_json(
        {
            "schema": "evm-storage/snapshot/v1",
            "address": address,
            "chain_id": chain_id,
            "block": block,
            "block_hash": block_hash,
            "layout": layout.to_dict(),
            "values": values,
            "evidence": evidence,
        }
    )
    return 0


def _diff(args: argparse.Namespace) -> int:
    before = _load_snapshot(Path(args.before))
    after = _load_snapshot(Path(args.after))
    before_address = normalize_address(str(before.get("address")))
    after_address = normalize_address(str(after.get("address")))
    if before_address != after_address:
        raise LayoutError("cannot diff snapshots of different contract addresses")
    before_chain = before.get("chain_id")
    after_chain = after.get("chain_id")
    if before_chain is not None and after_chain is not None and before_chain != after_chain:
        raise LayoutError("cannot diff snapshots from different chains")
    before_values = before["values"]
    after_values = after["values"]
    changes = [
        {"path": path, "before": before_values.get(path), "after": after_values.get(path)}
        for path in sorted(set(before_values) | set(after_values))
        if before_values.get(path) != after_values.get(path)
    ]
    result = {
        "schema": "evm-storage/snapshot-diff/v1",
        "address": after_address,
        "chain_id": after_chain if after_chain is not None else before_chain,
        "before_block": before.get("block"),
        "after_block": after.get("block"),
        "changes": changes,
    }
    if args.json_output:
        print_json(result)
    else:
        for change in changes:
            print(f"{change['path']}: {change['before']} -> {change['after']}")
        if not changes:
            print("no changes")
    return 0


def _normalize(args: argparse.Namespace) -> int:
    layout = load_layout_file(
        args.artifact,
        language=args.language,
        contract=args.contract,
        compiler_version=args.compiler_version,
    )
    _write_json(layout.to_dict(), args.output)
    return 0


def _extract_solidity(args: argparse.Namespace) -> int:
    layout = extract_solidity(
        args.source,
        solc=args.solc,
        contract=args.contract,
        base_path=args.base_path,
    )
    _write_json(layout.to_dict(), args.output)
    return 0


def _extract_vyper(args: argparse.Namespace) -> int:
    layout = extract_vyper(
        args.source,
        version=args.version,
        contract=args.contract,
        paths=args.path,
    )
    _write_json(layout.to_dict(), args.output)
    return 0


def _layout_map(values: list[str]) -> dict[str, StorageLayout]:
    layouts: dict[str, StorageLayout] = {}
    for value in values:
        if "=" not in value:
            raise LayoutError("--layout must be ADDRESS=ARTIFACT")
        address, path = value.split("=", 1)
        layouts[normalize_address(address)] = load_layout_file(path)
    return layouts


def _fetch_sourcify_for_block(
    rpc: RPCClient,
    chain_id: int,
    address: str,
    block: str,
    *,
    base_url: str,
) -> StorageLayout:
    layout = fetch_sourcify_layout(chain_id, address, base_url=base_url)
    provenance = layout.metadata.get("sourcify")
    expected = provenance.get("runtime_code_hash") if isinstance(provenance, dict) else None
    if not isinstance(expected, str):
        return _unvalidated_sourcify_layout(
            layout,
            "Sourcify response did not include a runtime bytecode identity",
        )
    try:
        code = rpc.code(address, block)
    except RPCError as exc:
        return _unvalidated_sourcify_layout(
            layout,
            f"historical runtime bytecode could not be read: {exc}",
        )
    try:
        raw = bytes.fromhex(code.removeprefix("0x"))
    except ValueError as exc:
        raise LayoutError(f"RPC returned malformed runtime bytecode for {address}") from exc
    actual = "0x" + keccak(raw).hex()
    if actual.lower() != expected.lower():
        raise LayoutError(
            f"Sourcify runtime bytecode for {address} does not match block {block}; layout rejected"
        )
    assert isinstance(provenance, dict)
    return replace(
        layout,
        metadata={
            **layout.metadata,
            "sourcify": {
                **provenance,
                "code_validation": "matched",
                "validated_block": block,
            },
        },
    )


def _unvalidated_sourcify_layout(layout: StorageLayout, reason: str) -> StorageLayout:
    return replace(
        layout,
        metadata={
            **layout.metadata,
            "attribution": "sourcify-unvalidated",
            "attribution_reason": reason,
        },
    )


def _layout_provenance(layout: StorageLayout) -> dict[str, Any]:
    return {
        "language": layout.language,
        "compiler_version": layout.compiler_version,
        "contract": layout.contract,
        "source": layout.source,
        "hash_order": layout.hash_order,
        "storage_dialect": layout.storage_dialect,
        "metadata": layout.metadata,
    }


def _rpc(args: argparse.Namespace) -> RPCClient:
    if args.rpc_max_response_mib <= 0:
        raise ValueError("--rpc-max-response-mib must be positive")
    return RPCClient(
        args.rpc_url,
        timeout=args.rpc_timeout,
        max_response_bytes=args.rpc_max_response_mib * 1024 * 1024,
    )


def _normalize_block_tag(value: str) -> str:
    lowered = value.lower()
    if lowered in {"earliest", "latest", "safe", "finalized", "pending"}:
        return lowered
    try:
        return hex(parse_int(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid block number or tag: {value}") from exc


def _pin_block(rpc: RPCClient, value: str) -> tuple[str, str]:
    requested = _normalize_block_tag(value)
    if requested == "pending":
        raise ValueError("storage reads cannot use the mutable pending block")
    header = rpc.block(requested)
    block = header.get("number")
    block_hash = header.get("hash")
    if not isinstance(block, str) or not isinstance(block_hash, str):
        raise RPCError(f"block {requested} has no stable number and hash")
    return block, block_hash


def _assert_block_unchanged(rpc: RPCClient, block: str, block_hash: str) -> None:
    if rpc.block(block).get("hash") != block_hash:
        raise RPCError(f"block {block} changed while storage was being read")


def _write_json(value: Any, output: str | None) -> None:
    if output is None or output == "-":
        print_json(value)
        return
    path = Path(output)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    print(path)


def _load_paths(path: Path) -> list[str]:
    try:
        text = path.read_text()
    except OSError as exc:
        raise LayoutError(f"could not read paths from {path}: {exc}") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = [
            stripped
            for line in text.splitlines()
            if (stripped := line.strip()) and not stripped.startswith("#")
        ]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise LayoutError("paths file must be a JSON string array or one path per line")
    return value


def _load_snapshot(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except OSError as exc:
        raise LayoutError(f"could not read snapshot {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LayoutError(f"snapshot {path} is not valid JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != "evm-storage/snapshot/v1"
        or not isinstance(value.get("values"), dict)
    ):
        raise LayoutError(f"{path} is not an evm-storage snapshot")
    return value


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
