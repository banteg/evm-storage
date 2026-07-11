"""Resolve raw changed slots into typed compiler-level paths."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from eth_hash.auto import keccak

from evm_storage.model import (
    Preimage,
    ResolvedChange,
    SlotChange,
    StorageLayout,
    StorageType,
    address_hex,
    word_hex,
)
from evm_storage.trace import TraceBundle

_UINT_RE = re.compile(r"(?:^|_)uint(?P<bits>[0-9]+)?(?:_|$)", re.IGNORECASE)
_INT_RE = re.compile(r"(?:^|_)int(?P<bits>[0-9]+)?(?:_|$)", re.IGNORECASE)
_BYTES_RE = re.compile(r"(?:^|_)bytes(?P<size>[0-9]+)(?:_|$)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Candidate:
    path: str
    type_id: str | None
    offset: int
    size: int
    confidence: str = "exact"
    reason: str | None = None
    label: str | None = None


class StorageResolver:
    def __init__(self, layout: StorageLayout, preimages: Iterable[Preimage] = ()):
        self.layout = layout
        self.preimages = tuple(preimages)
        self.by_hash = {item.hash: item for item in self.preimages}

    def resolve(
        self, change: SlotChange, *, layout_address: str | None = None
    ) -> tuple[ResolvedChange, ...]:
        candidates: list[_Candidate] = []
        relevant = tuple(
            item for item in self.preimages if item.storage_address in {None, change.address}
        )
        for variable in self.layout.variables:
            candidates.extend(
                self._find(
                    variable.type_id,
                    variable.slot,
                    change.slot,
                    variable.name,
                    initial_offset=variable.offset,
                    preimages=relevant,
                    seen=set(),
                )
            )

        out: list[ResolvedChange] = []
        if any(candidate.confidence == "exact" for candidate in candidates):
            candidates = [candidate for candidate in candidates if candidate.confidence == "exact"]
        seen_paths: set[tuple[str, str | None, int, int]] = set()
        for candidate in candidates:
            marker = (candidate.path, candidate.type_id, candidate.offset, candidate.size)
            if marker in seen_paths:
                continue
            seen_paths.add(marker)
            before_raw = _extract(change.before, candidate.offset, candidate.size)
            after_raw = _extract(change.after, candidate.offset, candidate.size)
            if before_raw == after_raw:
                continue
            type_ = self.layout.types.get(candidate.type_id) if candidate.type_id else None
            label = candidate.label or (type_.label if type_ else None)
            confidence = candidate.confidence
            reason = candidate.reason
            attribution = self.layout.metadata.get("attribution")
            if attribution == "static-proxy" and confidence == "exact":
                confidence = "partial"
                reason = (
                    "layout owner inferred from static proxy state; no SSTORE frame attribution"
                )
            elif attribution == "sourcify-unvalidated" and confidence == "exact":
                confidence = "partial"
                reason = str(
                    self.layout.metadata.get(
                        "attribution_reason",
                        "Sourcify layout could not be bound to historical runtime bytecode",
                    )
                )
            out.append(
                ResolvedChange(
                    change=change,
                    path=candidate.path,
                    type_label=label,
                    before=_decode_value(type_, before_raw, candidate.size, label),
                    after=_decode_value(type_, after_raw, candidate.size, label),
                    confidence=confidence,  # type: ignore[arg-type]
                    reason=reason,
                    layout_address=layout_address,
                )
            )
        if out:
            return tuple(out)
        return (
            ResolvedChange(
                change=change,
                path=None,
                type_label=None,
                before=word_hex(change.before),
                after=word_hex(change.after),
                confidence="unresolved",
                reason="no compiler path matched this slot with the observed preimages",
                layout_address=layout_address,
            ),
        )

    def _find(
        self,
        type_id: str,
        base: int,
        target: int,
        path: str,
        *,
        initial_offset: int = 0,
        preimages: tuple[Preimage, ...],
        seen: set[tuple[str, int, str]],
    ) -> list[_Candidate]:
        marker = (type_id, base, path)
        if marker in seen:
            return []
        seen = {*seen, marker}
        type_ = self.layout.types[type_id]

        if type_.encoding == "mapping":
            return self._mapping(type_, base, target, path, preimages, seen)
        if type_.encoding == "dynamic_array":
            return self._solidity_dynamic_array(type_, base, target, path, preimages, seen)
        if type_.encoding == "vyper_dynamic_array":
            transformed = self._composite_base(type_, base)
            if target == transformed:
                return [_Candidate(f"{path}.length", None, 0, 32, label="uint256")]
            return self._array_elements(
                type_, transformed + 1, target, path, preimages, seen, dynamic=True
            )
        if type_.encoding == "bytes":
            return self._solidity_bytes(type_, base, target, path)
        if type_.encoding == "vyper_bytes":
            transformed = self._composite_base(type_, base)
            if target == transformed:
                return [_Candidate(f"{path}.length", None, 0, 32, label="uint256")]
            maximum_words = max(0, type_.words - 1)
            if transformed + 1 <= target < transformed + 1 + maximum_words:
                index = target - transformed - 1
                return [
                    _Candidate(
                        f"{path}.data[{index}]",
                        None,
                        0,
                        32,
                        label="bytes32",
                    )
                ]
            return []

        composite = bool(type_.members or (type_.base is not None and type_.length is not None))
        if composite:
            base = self._composite_base(type_, base)
        if type_.members:
            found: list[_Candidate] = []
            for member in type_.members:
                found.extend(
                    self._find(
                        member.type_id,
                        (base + member.slot) % (1 << 256),
                        target,
                        f"{path}.{member.name}",
                        initial_offset=member.offset,
                        preimages=preimages,
                        seen=seen,
                    )
                )
            return found
        if type_.base is not None and type_.length is not None:
            return self._array_elements(type_, base, target, path, preimages, seen, dynamic=False)
        if type_.encoding == "opaque":
            if base <= target < base + type_.words:
                suffix = "" if type_.words == 1 else f"[slot+{target - base}]"
                return [
                    _Candidate(
                        path + suffix,
                        type_id,
                        0,
                        32,
                        confidence="partial",
                        reason="compiler type is opaque",
                    )
                ]
            return []
        if target == base:
            return [
                _Candidate(
                    path,
                    type_id,
                    initial_offset,
                    min(32 - initial_offset, max(1, type_.number_of_bytes)),
                )
            ]
        return []

    def _mapping(
        self,
        type_: StorageType,
        base: int,
        target: int,
        path: str,
        preimages: tuple[Preimage, ...],
        seen: set[tuple[str, int, str]],
    ) -> list[_Candidate]:
        assert type_.key is not None and type_.value is not None
        found: list[_Candidate] = []
        key_type = self.layout.types[type_.key]
        for preimage in preimages:
            data = preimage.data
            if len(data) < 32:
                continue
            if self.layout.hash_order == "key-slot":
                key_data = data[:-32]
                parent = int.from_bytes(data[-32:], "big")
            else:
                parent = int.from_bytes(data[:32], "big")
                key_data = data[32:]
            if parent != base:
                continue
            dynamic_key = _is_dynamic_mapping_key(key_type)
            raw_dynamic = dynamic_key and self.layout.language == "solidity"
            if not dynamic_key and len(key_data) != 32:
                continue
            if dynamic_key and not raw_dynamic and len(key_data) != 32:
                continue
            key_text = _decode_mapping_key(
                key_type,
                key_data,
                self.by_hash,
                raw_dynamic=raw_dynamic,
            )
            if key_text is None:
                continue
            found.extend(
                self._find(
                    type_.value,
                    preimage.hash,
                    target,
                    f"{path}[{key_text}]",
                    preimages=preimages,
                    seen=seen,
                )
            )
        return found

    def _solidity_dynamic_array(
        self,
        type_: StorageType,
        base: int,
        target: int,
        path: str,
        preimages: tuple[Preimage, ...],
        seen: set[tuple[str, int, str]],
    ) -> list[_Candidate]:
        if target == base:
            return [_Candidate(f"{path}.length", None, 0, 32, label="uint256")]
        data = _hash_word(base)
        observed = self.by_hash.get(data)
        if observed is None or observed.data != base.to_bytes(32, "big"):
            return []
        return self._array_elements(type_, data, target, path, preimages, seen, dynamic=True)

    def _array_elements(
        self,
        type_: StorageType,
        base: int,
        target: int,
        path: str,
        preimages: tuple[Preimage, ...],
        seen: set[tuple[str, int, str]],
        *,
        dynamic: bool,
    ) -> list[_Candidate]:
        assert type_.base is not None
        element = self.layout.types[type_.base]
        limit = type_.length
        if (
            self.layout.storage_dialect == "vyper-hashed-composites"
            and limit is not None
            and _has_indirect_legacy_root(element)
        ):
            indices = {
                parent - base
                for preimage in preimages
                if len(preimage.data) >= 32
                and base <= (parent := int.from_bytes(preimage.data[:32], "big")) < base + limit
            }
            found: list[_Candidate] = []
            for index in sorted(indices):
                found.extend(
                    self._find(
                        type_.base,
                        base + index,
                        target,
                        f"{path}[{index}]",
                        preimages=preimages,
                        seen=seen,
                    )
                )
            return found
        if self.layout.language == "solidity" and _packable(element):
            size = element.number_of_bytes
            per_word = 32 // size
            if target < base:
                return []
            word_index = target - base
            first = word_index * per_word
            if limit is not None and first >= limit:
                return []
            count = per_word if limit is None else min(per_word, limit - first)
            candidates = [
                _Candidate(f"{path}[{first + index}]", type_.base, index * size, size)
                for index in range(count)
            ]
            return _unbounded_array_candidates(candidates) if dynamic else candidates

        words = 1 if self.layout.storage_dialect == "vyper-hashed-composites" else element.words
        if target < base:
            return []
        index = (target - base) // words
        if limit is not None and index >= limit:
            return []
        element_base = base + index * words
        candidates = self._find(
            type_.base,
            element_base,
            target,
            f"{path}[{index}]",
            preimages=preimages,
            seen=seen,
        )
        return _unbounded_array_candidates(candidates) if dynamic else candidates

    def _solidity_bytes(
        self, type_: StorageType, base: int, target: int, path: str
    ) -> list[_Candidate]:
        if target == base:
            return [
                _Candidate(
                    path,
                    type_.id,
                    0,
                    32,
                    confidence="partial",
                    reason="bytes/string head contains inline data or external length",
                )
            ]
        data = _hash_word(base)
        observed = self.by_hash.get(data)
        if observed is None or observed.data != base.to_bytes(32, "big"):
            return []
        if target >= data:
            index = target - data
            return [
                _Candidate(
                    f"{path}.data[{index}]",
                    None,
                    0,
                    32,
                    confidence="partial",
                    reason="one long bytes/string data word",
                    label="bytes32",
                )
            ]
        return []

    def _composite_base(self, type_: StorageType, base: int) -> int:
        if self.layout.storage_dialect == "vyper-hashed-composites" and (
            type_.members
            or (type_.base is not None and type_.length is not None)
            or type_.encoding == "vyper_bytes"
        ):
            return _hash_word(base)
        return base


def resolve_bundle(
    bundle: TraceBundle, layouts: dict[str, StorageLayout]
) -> tuple[ResolvedChange, ...]:
    normalized = {address.lower(): layout for address, layout in layouts.items()}
    out: list[ResolvedChange] = []
    for change in bundle.changes:
        candidates: list[tuple[str, StorageLayout]] = []
        if change.write is not None and change.write.code_address in normalized:
            candidates.append((change.write.code_address, normalized[change.write.code_address]))
        if change.address in normalized and all(
            address != change.address for address, _ in candidates
        ):
            candidates.append((change.address, normalized[change.address]))
        if not candidates:
            out.append(
                ResolvedChange(
                    change=change,
                    path=None,
                    type_label=None,
                    before=word_hex(change.before),
                    after=word_hex(change.after),
                    confidence="unresolved",
                    reason="no layout supplied for storage or executing code address",
                )
            )
            continue
        best: tuple[ResolvedChange, ...] | None = None
        best_rank = -1
        for address, layout in candidates:
            resolver = StorageResolver(layout, bundle.preimages)
            resolved = resolver.resolve(change, layout_address=address)
            rank = max(
                {"unresolved": 0, "partial": 1, "exact": 2}[item.confidence] for item in resolved
            )
            if rank > best_rank:
                best = resolved
                best_rank = rank
            if rank == 2:
                break
        out.extend(best or ())
    return tuple(out)


def _hash_word(value: int) -> int:
    return int.from_bytes(keccak(value.to_bytes(32, "big")), "big")


def _packable(type_: StorageType) -> bool:
    return (
        type_.encoding == "inplace"
        and not type_.members
        and type_.base is None
        and 0 < type_.number_of_bytes < 32
    )


def _has_indirect_legacy_root(type_: StorageType) -> bool:
    return bool(
        type_.members
        or type_.base is not None
        or type_.encoding in {"mapping", "vyper_bytes", "vyper_dynamic_array"}
    )


def _extract(word: int, offset: int, size: int) -> int:
    if size >= 32 and offset == 0:
        return word
    mask = (1 << (size * 8)) - 1
    return (word >> (offset * 8)) & mask


def _decode_value(type_: StorageType | None, value: int, size: int, label_hint: str | None) -> Any:
    label = (label_hint or (type_.label if type_ else "")).strip()
    lowered = label.lower()
    if type_ is not None and type_.encoding == "bytes" and size == 32:
        if value & 1:
            return {"length": (value - 1) // 2, "data": "external"}
        length = (value & 0xFF) // 2
        data = value.to_bytes(32, "big")[:length]
        if lowered.startswith("string"):
            try:
                return data.decode()
            except UnicodeDecodeError:
                return "0x" + data.hex()
        return "0x" + data.hex()
    if lowered == "bool" or "t_bool" in lowered:
        return bool(value)
    if lowered == "address" or "address" in lowered or lowered.startswith("contract "):
        try:
            return address_hex(value)
        except ValueError:
            return word_hex(value)
    fixed = _BYTES_RE.search(lowered)
    if fixed:
        width = int(fixed.group("size"))
        if type_ is not None and type_.id.startswith("vyper:"):
            return "0x" + value.to_bytes(32, "big")[:width].hex()
        return "0x" + value.to_bytes(width, "big").hex()
    if lowered == "bytes32":
        return word_hex(value)
    signed = _INT_RE.search(lowered)
    if signed and not _UINT_RE.search(lowered):
        bits = int(signed.group("bits") or size * 8)
        value &= (1 << bits) - 1
        sign = 1 << (bits - 1)
        return value - (1 << bits) if value & sign else value
    if lowered == "decimal":
        value &= (1 << 168) - 1
        sign = 1 << 167
        raw = value - (1 << 168) if value & sign else value
        prefix = "-" if raw < 0 else ""
        whole, fractional = divmod(abs(raw), 10**10)
        return f"{prefix}{whole}.{fractional:010d}"
    if _UINT_RE.search(lowered) or lowered.startswith(("enum ", "flag ")):
        return value
    if label in {"bytes32", "Bytes[32]"}:
        return word_hex(value)
    return value


def _decode_mapping_key(
    type_: StorageType,
    data: bytes,
    preimages: dict[int, Preimage],
    *,
    raw_dynamic: bool,
) -> str | None:
    label = type_.label.lower()
    if _is_dynamic_mapping_key(type_):
        if raw_dynamic:
            try:
                return repr(data.decode()) if label.startswith("string") else "0x" + data.hex()
            except UnicodeDecodeError:
                return "0x" + data.hex()
        if len(data) != 32:
            return None
        value = int.from_bytes(data, "big")
        nested = preimages.get(value)
        if nested is not None:
            try:
                return (
                    repr(nested.data.decode())
                    if label.startswith("string")
                    else "0x" + nested.data.hex()
                )
            except UnicodeDecodeError:
                return "0x" + nested.data.hex()
        return f"keccak:{word_hex(value)}"
    if len(data) != 32:
        return None
    value = int.from_bytes(data, "big")
    if "address" in label:
        if value >= 1 << 160:
            return None
        try:
            return address_hex(value)
        except ValueError:
            return None
    if _INT_RE.search(label) and not _UINT_RE.search(label):
        bits_match = _INT_RE.search(label)
        bits = int(bits_match.group("bits") or 256) if bits_match else 256
        mask = (1 << bits) - 1
        low = value & mask
        signed = low - (1 << bits) if low & (1 << (bits - 1)) else low
        if value != signed & ((1 << 256) - 1):
            return None
        return str(signed)
    if _UINT_RE.search(label) or label == "bool" or label.startswith(("enum ", "flag ")):
        if label == "bool" and value not in {0, 1}:
            return None
        bits_match = _UINT_RE.search(label)
        bits = (
            int(bits_match.group("bits") or 256)
            if bits_match
            else min(256, max(8, type_.number_of_bytes * 8))
        )
        if value >= 1 << bits:
            return None
        return str(value)
    fixed = _BYTES_RE.search(label)
    if fixed:
        width = int(fixed.group("size"))
        if any(data[width:]):
            return None
        return "0x" + data[:width].hex()
    return word_hex(value)


def _is_dynamic_mapping_key(type_: StorageType) -> bool:
    label = type_.label.lower()
    return type_.encoding in {"bytes", "vyper_bytes"} or label.startswith(("string", "bytes["))


def _unbounded_array_candidates(candidates: list[_Candidate]) -> list[_Candidate]:
    reason = "dynamic array element inferred without an authoritative runtime length"
    return [
        replace(
            candidate,
            confidence="partial",
            reason=f"{candidate.reason}; {reason}" if candidate.reason else reason,
        )
        for candidate in candidates
    ]
