"""Normalize state diffs and Geth/Reth struct logs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from eth_hash.auto import keccak

from evm_storage.errors import RPCError, TraceError
from evm_storage.model import (
    ZERO_ADDRESS,
    Preimage,
    SlotChange,
    WriteContext,
    normalize_address,
    parse_int,
)
from evm_storage.rpc import RPCClient

_OPCODES = {
    0x00: "STOP",
    0x20: "KECCAK256",
    0x55: "SSTORE",
    0xF0: "CREATE",
    0xF1: "CALL",
    0xF2: "CALLCODE",
    0xF3: "RETURN",
    0xF4: "DELEGATECALL",
    0xF5: "CREATE2",
    0xFA: "STATICCALL",
    0xFD: "REVERT",
}
_CALL_OPS = {"CALL", "CALLCODE", "DELEGATECALL", "STATICCALL"}
_DELEGATING_OPS = {"CALLCODE", "DELEGATECALL"}
_MAX_KECCAK_PREIMAGE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Frame:
    depth: int
    call_type: str
    call_target: str
    code_address: str
    storage_address: str


@dataclass(frozen=True, slots=True)
class TraceEvidence:
    preimages: tuple[Preimage, ...]
    writes: tuple[WriteContext, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TraceBundle:
    tx_hash: str
    transaction: dict[str, Any]
    receipt: dict[str, Any]
    changes: tuple[SlotChange, ...]
    preimages: tuple[Preimage, ...]
    writes: tuple[WriteContext, ...]
    client_version: str | None
    warnings: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)


class TraceProvider:
    def __init__(self, rpc: RPCClient):
        self.rpc = rpc

    def transaction(self, tx_hash: str) -> TraceBundle:
        tx = self.rpc.transaction(tx_hash)
        receipt = self.rpc.receipt(tx_hash)
        block_hash = receipt.get("blockHash")
        warnings: list[str] = []
        client_version: str | None
        try:
            client_version = self.rpc.client_version()
        except RPCError as exc:
            client_version = None
            warnings.append(str(exc))

        changes: list[SlotChange]
        state_backend = "debug-prestateTracer"
        prestate: dict[str, Any] | None = None
        try:
            prestate = self.rpc.call(
                "debug_traceTransaction",
                [
                    tx_hash,
                    {
                        "tracer": "prestateTracer",
                        "tracerConfig": {"diffMode": True, "disableCode": False},
                    },
                ],
            )
            changes = parse_prestate_diff(prestate)
        except (RPCError, TraceError) as exc:
            warnings.append(f"prestateTracer unavailable: {exc}")
            state_backend = "trace_replayTransaction"
            replay = self.rpc.call("trace_replayTransaction", [tx_hash, ["stateDiff"]])
            changes = parse_parity_diff(replay)

        evidence = TraceEvidence((), ())
        opcode_backend = "unavailable"
        try:
            struct = self.rpc.call(
                "debug_traceTransaction",
                [
                    tx_hash,
                    {
                        "enableMemory": True,
                        "disableStack": False,
                        "disableStorage": True,
                        "enableReturnData": False,
                    },
                ],
            )
            logs = struct.get("structLogs") if isinstance(struct, dict) else None
            if not isinstance(logs, list):
                raise TraceError("struct logger returned no structLogs array")
            root = tx.get("to") or ZERO_ADDRESS
            block = tx.get("blockNumber") or "latest"
            delegation = _delegation_resolver(self.rpc, block, prestate)
            evidence = analyze_struct_logs(logs, root_address=root, resolve_code=delegation)
            opcode_backend = "debug-structLogger"
            warnings.extend(evidence.warnings)
        except (RPCError, TraceError, TypeError, ValueError) as exc:
            warnings.append(f"opcode evidence unavailable: {exc}")

        matching_writes: dict[tuple[str, int], list[WriteContext]] = {}
        for write in evidence.writes:
            matching_writes.setdefault((write.storage_address, write.slot), []).append(write)

        def final_writer(change: SlotChange) -> WriteContext | None:
            writes = matching_writes.get((change.address, change.slot), [])
            exact = [write for write in writes if write.value == change.after]
            if not exact:
                if writes:
                    warnings.append(
                        f"no observed SSTORE wrote the final value for "
                        f"{change.address}:{change.slot:#x}"
                    )
                return None
            contexts = {(write.code_address, write.storage_address) for write in exact}
            if len(contexts) != 1:
                warnings.append(
                    f"ambiguous final SSTORE attribution for {change.address}:{change.slot:#x}"
                )
                return None
            return exact[-1]

        attributed = tuple(
            SlotChange(
                address=change.address,
                slot=change.slot,
                before=change.before,
                after=change.after,
                write=final_writer(change),
            )
            for change in changes
        )

        current_receipt = self.rpc.receipt(tx_hash)
        if block_hash is not None and current_receipt.get("blockHash") != block_hash:
            raise TraceError("transaction was reorganized while tracing")
        return TraceBundle(
            tx_hash=tx_hash,
            transaction=tx,
            receipt=receipt,
            changes=attributed,
            preimages=evidence.preimages,
            writes=evidence.writes,
            client_version=client_version,
            warnings=tuple(warnings),
            provenance={"state_diff": state_backend, "opcodes": opcode_backend},
        )


def parse_prestate_diff(value: object) -> list[SlotChange]:
    if not isinstance(value, dict):
        raise TraceError("prestate diff is not an object")
    pre = value.get("pre")
    post = value.get("post")
    if not isinstance(pre, dict) or not isinstance(post, dict):
        raise TraceError("prestate tracer did not return diff mode pre/post objects")
    changes: list[SlotChange] = []
    addresses = set(pre) | set(post)
    for address in addresses:
        before_account = pre.get(address, {})
        after_account = post.get(address, {})
        if not isinstance(before_account, dict) or not isinstance(after_account, dict):
            continue
        before_storage = before_account.get("storage", {})
        after_storage = after_account.get("storage", {})
        if not isinstance(before_storage, dict) or not isinstance(after_storage, dict):
            continue
        for slot_text in set(before_storage) | set(after_storage):
            slot = parse_int(slot_text)
            before = parse_int(before_storage.get(slot_text, 0))
            after = parse_int(after_storage.get(slot_text, 0))
            if before != after:
                changes.append(SlotChange(normalize_address(address), slot, before, after))
    return sorted(changes, key=lambda item: (item.address, item.slot))


def parse_parity_diff(value: object) -> list[SlotChange]:
    if not isinstance(value, dict):
        raise TraceError("Parity replay result is not an object")
    state = value.get("stateDiff", value)
    if not isinstance(state, dict):
        raise TraceError("Parity replay returned no stateDiff")
    changes: list[SlotChange] = []
    for address, account in state.items():
        if not isinstance(account, dict) or not isinstance(account.get("storage"), dict):
            continue
        for slot_text, delta in account["storage"].items():
            if delta == "=" or not isinstance(delta, dict):
                continue
            if "*" in delta and isinstance(delta["*"], dict):
                before = parse_int(delta["*"].get("from", 0))
                after = parse_int(delta["*"].get("to", 0))
            elif "+" in delta:
                before, after = 0, parse_int(delta["+"])
            elif "-" in delta:
                before, after = parse_int(delta["-"]), 0
            else:
                continue
            if before != after:
                changes.append(
                    SlotChange(normalize_address(address), parse_int(slot_text), before, after)
                )
    return sorted(changes, key=lambda item: (item.address, item.slot))


def analyze_struct_logs(
    logs: list[object],
    *,
    root_address: str,
    resolve_code: Callable[[str], str] | None = None,
) -> TraceEvidence:
    if not logs:
        return TraceEvidence((), (), ("empty opcode trace",))
    first = logs[0]
    if not isinstance(first, dict):
        raise TraceError("struct log contains a non-object entry")
    first_depth = int(first.get("depth", 1))
    root = normalize_address(root_address)
    root_code = resolve_code(root) if resolve_code is not None else root
    frames: dict[int, Frame] = {first_depth: Frame(first_depth, "ROOT", root, root_code, root)}
    current_depth = first_depth
    pending: Frame | None = None
    preimages: list[Preimage] = []
    writes: list[WriteContext] = []
    warnings: list[str] = []

    for raw in logs:
        if not isinstance(raw, dict):
            raise TraceError("struct log contains a non-object entry")
        depth = int(raw.get("depth", current_depth))
        if depth > current_depth:
            if pending is None:
                warnings.append(f"depth rose to {depth} without a call opcode")
                parent = frames[current_depth]
                pending = Frame(
                    depth, "UNKNOWN", ZERO_ADDRESS, ZERO_ADDRESS, parent.storage_address
                )
            frames[depth] = Frame(
                depth,
                pending.call_type,
                pending.call_target,
                pending.code_address,
                pending.storage_address,
            )
        elif depth < current_depth:
            for old_depth in [item for item in frames if item > depth]:
                frames.pop(old_depth, None)
        elif pending is not None:
            pending = None
        current_depth = depth
        frame = frames.get(depth)
        if frame is None:
            raise TraceError(f"missing execution frame for depth {depth}")
        opcode = _opcode(raw)
        stack = _stack(raw.get("stack"))

        if opcode in {"KECCAK256", "SHA3"}:
            if len(stack) < 2:
                warnings.append(f"KECCAK256 at depth {depth} has no stack")
            else:
                offset, size = stack[-1], stack[-2]
                end = offset + size
                if size > _MAX_KECCAK_PREIMAGE_BYTES or end > _MAX_KECCAK_PREIMAGE_BYTES:
                    warnings.append(
                        f"KECCAK256 at depth {depth} exceeds the explicit "
                        f"{_MAX_KECCAK_PREIMAGE_BYTES}-byte preimage limit"
                    )
                    continue
                memory = _memory(raw.get("memory"))
                if end > len(memory):
                    memory += b"\x00" * (end - len(memory))
                data = memory[offset:end]
                preimages.append(
                    Preimage(
                        hash=int.from_bytes(keccak(data), "big"),
                        data=data,
                        depth=depth,
                        code_address=frame.code_address,
                        storage_address=frame.storage_address,
                    )
                )
        elif opcode == "SSTORE":
            if len(stack) < 2:
                warnings.append(f"SSTORE at depth {depth} has no stack")
            else:
                writes.append(
                    WriteContext(
                        slot=stack[-1],
                        value=stack[-2],
                        depth=depth,
                        code_address=frame.code_address,
                        storage_address=frame.storage_address,
                        pc=_optional_int(raw.get("pc")),
                    )
                )

        if opcode in _CALL_OPS and len(stack) >= 2:
            target = normalize_address(hex(stack[-2] & ((1 << 160) - 1)))
            code = resolve_code(target) if resolve_code is not None else target
            storage = frame.storage_address if opcode in _DELEGATING_OPS else target
            pending = Frame(depth + 1, opcode, target, code, storage)
        elif opcode in {"CREATE", "CREATE2"}:
            pending = Frame(depth + 1, opcode, ZERO_ADDRESS, ZERO_ADDRESS, ZERO_ADDRESS)

    return TraceEvidence(tuple(preimages), tuple(writes), tuple(dict.fromkeys(warnings)))


def _opcode(log: dict[str, Any]) -> str:
    op_name = log.get("opName")
    if isinstance(op_name, str):
        return op_name.upper()
    op = log.get("op")
    if isinstance(op, str):
        if op.startswith("0x"):
            try:
                return _OPCODES.get(int(op, 16), op.upper())
            except ValueError:
                return op.upper()
        return op.upper()
    if isinstance(op, int):
        return _OPCODES.get(op, f"0x{op:02X}")
    return "UNKNOWN"


def _stack(value: object) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TraceError("struct-log stack is not an array")
    out: list[int] = []
    for item in value:
        if isinstance(item, str) and not item.startswith("0x"):
            item = "0x" + item
        out.append(parse_int(item))  # type: ignore[arg-type]
    return out


def _memory(value: object) -> bytes:
    if value is None:
        return b""
    if isinstance(value, str):
        text = value.removeprefix("0x")
        if len(text) % 2:
            text = "0" + text
        try:
            return bytes.fromhex(text)
        except ValueError as exc:
            raise TraceError("struct-log memory contains invalid hex") from exc
    if isinstance(value, list):
        chunks: list[bytes] = []
        for word in value:
            if not isinstance(word, str):
                raise TraceError("struct-log memory word is not hex")
            text = word.removeprefix("0x")
            if len(text) % 2:
                text = "0" + text
            try:
                chunks.append(bytes.fromhex(text))
            except ValueError as exc:
                raise TraceError("struct-log memory contains invalid hex") from exc
        return b"".join(chunks)
    raise TraceError("struct-log memory has an unsupported shape")


def _delegation_resolver(
    rpc: RPCClient, block: str, prestate: dict[str, Any] | None
) -> Callable[[str], str]:
    cache: dict[str, str] = {}
    post = prestate.get("post", {}) if isinstance(prestate, dict) else {}
    normalized_post = (
        {normalize_address(key): item for key, item in post.items()}
        if isinstance(post, dict)
        else {}
    )

    def resolve(address: str) -> str:
        address = normalize_address(address)
        if address in cache:
            return cache[address]
        code = None
        if isinstance(normalized_post.get(address), dict):
            candidate = normalized_post[address].get("code")
            if isinstance(candidate, str):
                code = candidate
        if code is None:
            try:
                code = rpc.code(address, block)
            except RPCError:
                code = "0x"
        raw = code.removeprefix("0x").lower()
        if len(raw) == 46 and raw.startswith("ef0100"):
            result = normalize_address("0x" + raw[6:46])
        else:
            result = address
        cache[address] = result
        return result

    return resolve


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        return int(value, 16 if value.startswith("0x") else 10)
    return int(value)
