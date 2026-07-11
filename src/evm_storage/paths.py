"""Forward path-to-slot calculation for explicit storage reads."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from eth_hash.auto import keccak

from evm_storage.errors import LayoutError
from evm_storage.model import StorageLayout, StorageType, parse_int

_TOKEN_RE = re.compile(r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)|\[(?P<key>[^]]+)]")
_FIXED_BYTES_RE = re.compile(r"^bytes(?P<size>[0-9]+)$", re.IGNORECASE)
_SIGNED_INT_RE = re.compile(r"^int(?P<bits>[0-9]+)?$", re.IGNORECASE)
_UNSIGNED_INT_RE = re.compile(r"^uint(?P<bits>[0-9]+)?$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class StorageLocation:
    path: str
    slot: int
    offset: int
    size: int
    type_id: str


def locate(layout: StorageLayout, path: str) -> StorageLocation:
    tokens = _tokens(path)
    if not tokens or tokens[0][0] != "name":
        raise LayoutError(f"invalid storage path: {path}")
    root_name = tokens.pop(0)[1]
    variable = next((item for item in layout.variables if item.name == root_name), None)
    if variable is None:
        raise LayoutError(f"unknown storage variable: {root_name}")
    slot = variable.slot
    offset = variable.offset
    type_id = variable.type_id

    while tokens:
        type_ = layout.types[type_id]
        if type_.encoding == "mapping":
            kind, raw_key = tokens.pop(0)
            if kind != "key":
                raise LayoutError(f"mapping {type_.label} requires [key]")
            assert type_.key is not None and type_.value is not None
            key = _encode_key(layout, layout.types[type_.key], raw_key)
            parent = slot.to_bytes(32, "big")
            encoded = key + parent if layout.hash_order == "key-slot" else parent + key
            slot = int.from_bytes(keccak(encoded), "big")
            offset = 0
            type_id = type_.value
            continue

        if type_.members:
            slot = _composite_root(layout, type_, slot)
            kind, name = tokens.pop(0)
            if kind != "name":
                raise LayoutError(f"struct {type_.label} requires .member")
            member = next((item for item in type_.members if item.name == name), None)
            if member is None:
                raise LayoutError(f"{type_.label} has no member {name!r}")
            slot = (slot + member.slot) % (1 << 256)
            offset = member.offset
            type_id = member.type_id
            continue

        if type_.base is not None:
            kind, raw_index = tokens.pop(0)
            if kind != "key":
                raise LayoutError(f"array {type_.label} requires [index]")
            try:
                index = parse_int(raw_index.strip())
            except (TypeError, ValueError) as exc:
                raise LayoutError(f"invalid array index: {raw_index}") from exc
            if type_.length is not None and index >= type_.length:
                raise LayoutError(f"array index {index} is outside {type_.label}")
            element = layout.types[type_.base]
            if type_.encoding == "dynamic_array":
                slot = int.from_bytes(keccak(slot.to_bytes(32, "big")), "big")
            elif type_.encoding == "vyper_dynamic_array":
                slot = _composite_root(layout, type_, slot) + 1
            else:
                slot = _composite_root(layout, type_, slot)
            if layout.language == "solidity" and _packable(element):
                per_word = 32 // element.number_of_bytes
                slot += index // per_word
                offset = (index % per_word) * element.number_of_bytes
            else:
                stride = 1 if layout.storage_dialect == "vyper-hashed-composites" else element.words
                slot += index * stride
                offset = 0
            type_id = type_.base
            continue
        raise LayoutError(f"cannot descend through scalar type {type_.label}")

    type_ = layout.types[type_id]
    if (
        type_.encoding
        in {
            "mapping",
            "dynamic_array",
            "vyper_dynamic_array",
            "bytes",
            "vyper_bytes",
        }
        or type_.members
        or type_.base is not None
    ):
        raise LayoutError(f"path ends at composite type {type_.label}; select a scalar member")
    return StorageLocation(
        path=path,
        slot=slot,
        offset=offset,
        size=min(32 - offset, max(1, type_.number_of_bytes)),
        type_id=type_id,
    )


def decode_location(layout: StorageLayout, location: StorageLocation, word: int):
    from evm_storage.resolver import _decode_value, _extract

    type_ = layout.types[location.type_id]
    raw = _extract(word, location.offset, location.size)
    return _decode_value(type_, raw, location.size, type_.label)


def _tokens(path: str) -> list[tuple[str, str]]:
    normalized = path.strip()
    tokens: list[tuple[str, str]] = []
    position = 0
    for match in _TOKEN_RE.finditer(normalized):
        between = normalized[position : match.start()]
        if between not in {"", "."}:
            raise LayoutError(f"invalid storage path near {between!r}")
        if match.group("name") is not None:
            tokens.append(("name", match.group("name")))
        else:
            tokens.append(("key", match.group("key")))
        position = match.end()
    if position != len(normalized):
        raise LayoutError(f"invalid storage path near {normalized[position:]!r}")
    return tokens


def _encode_key(layout: StorageLayout, type_: StorageType, text: str) -> bytes:
    value = text.strip()
    label = type_.label.lower()
    if type_.encoding in {"bytes", "vyper_bytes"} or label.startswith(("string", "bytes[")):
        raw = _bytes_key(value)
        return raw if layout.language == "solidity" else keccak(raw)

    fixed = _FIXED_BYTES_RE.fullmatch(label)
    if fixed:
        width = int(fixed.group("size"))
        if not 1 <= width <= 32:
            raise LayoutError(f"invalid fixed-bytes mapping key type: {type_.label}")
        raw = _fixed_bytes_key(value, width)
        return raw.ljust(32, b"\x00")

    if label == "bool":
        lowered = value.lower()
        if lowered in {"true", "false"}:
            return (lowered == "true").to_bytes(32, "big")
        integer = _key_integer(value, type_.label)
        if integer not in {0, 1}:
            raise LayoutError(f"mapping key {text!r} is outside bool")
        return integer.to_bytes(32, "big")

    signed = _SIGNED_INT_RE.fullmatch(label)
    if signed:
        bits = int(signed.group("bits") or 256)
        integer = _key_integer(value, type_.label)
        if not -(1 << (bits - 1)) <= integer < 1 << (bits - 1):
            raise LayoutError(f"mapping key {text!r} is outside {type_.label}")
        return (integer & ((1 << 256) - 1)).to_bytes(32, "big")

    try:
        integer = _key_integer(value, type_.label)
    except LayoutError:
        raise
    if integer < 0:
        raise LayoutError(f"mapping key {text!r} is negative for {type_.label}")
    unsigned = _UNSIGNED_INT_RE.fullmatch(label)
    if unsigned:
        bits = int(unsigned.group("bits") or 256)
    elif "address" in label or label.startswith("contract "):
        bits = 160
    else:
        bits = min(256, max(8, type_.number_of_bytes * 8))
    if integer >= 1 << bits:
        raise LayoutError(f"mapping key {text!r} is outside {type_.label}")
    return integer.to_bytes(32, "big")


def _bytes_key(value: str) -> bytes:
    if value.lower().startswith("0x"):
        text = value[2:]
        if len(text) % 2:
            raise LayoutError("hexadecimal bytes mapping key must contain whole bytes")
        try:
            return bytes.fromhex(text)
        except ValueError as exc:
            raise LayoutError("bytes mapping key contains invalid hexadecimal") from exc
    try:
        literal = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        literal = value
    if isinstance(literal, str):
        return literal.encode()
    if isinstance(literal, bytes):
        return literal
    raise LayoutError(f"dynamic mapping key must be bytes or string: {value}")


def _fixed_bytes_key(value: str, width: int) -> bytes:
    if value.lower().startswith("0x"):
        raw = _bytes_key(value)
    else:
        try:
            literal = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            literal = None
        if isinstance(literal, bytes):
            raw = literal
        elif isinstance(literal, str):
            raw = literal.encode()
        else:
            integer = _key_integer(value, f"bytes{width}")
            if integer < 0 or integer >= 1 << (width * 8):
                raise LayoutError(f"mapping key {value!r} is outside bytes{width}")
            raw = integer.to_bytes(width, "big")
    if len(raw) > width:
        raise LayoutError(f"mapping key is wider than bytes{width}")
    return raw.ljust(width, b"\x00")


def _key_integer(value: str, label: str) -> int:
    try:
        return int(value, 0 if value.lower().startswith(("0x", "+0x", "-0x")) else 10)
    except ValueError as exc:
        raise LayoutError(f"invalid mapping key {value!r} for {label}") from exc


def _composite_root(layout: StorageLayout, type_: StorageType, slot: int) -> int:
    if layout.storage_dialect == "vyper-hashed-composites" and (
        type_.members or type_.base is not None or type_.encoding == "vyper_bytes"
    ):
        return int.from_bytes(keccak(slot.to_bytes(32, "big")), "big")
    return slot


def _packable(type_: StorageType) -> bool:
    return (
        type_.encoding == "inplace"
        and not type_.members
        and type_.base is None
        and 0 < type_.number_of_bytes < 32
    )
