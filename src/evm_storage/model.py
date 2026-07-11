"""Stable, language-neutral storage layout and trace models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

UINT256_LIMIT = 1 << 256
ZERO_ADDRESS = "0x" + "00" * 20

Encoding = Literal[
    "inplace",
    "mapping",
    "dynamic_array",
    "bytes",
    "vyper_dynamic_array",
    "vyper_bytes",
    "opaque",
]
HashOrder = Literal["key-slot", "slot-key"]
StorageDialect = Literal["solidity", "vyper-hashed-composites", "vyper-inline"]


def parse_int(value: int | str | bytes) -> int:
    """Parse an EVM integer from a JSON-RPC or compiler representation."""
    if isinstance(value, bool):
        raise TypeError("boolean is not an EVM integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, bytes):
        result = int.from_bytes(value, "big")
    elif isinstance(value, str):
        result = int(value, 16 if value.startswith(("0x", "0X")) else 10)
    else:  # pragma: no cover - guarded by public type hints
        raise TypeError(f"unsupported integer value: {type(value).__name__}")
    if not 0 <= result < UINT256_LIMIT:
        raise ValueError("EVM integer is outside uint256 range")
    return result


def word_hex(value: int) -> str:
    return f"0x{value:064x}"


def address_hex(value: int | str) -> str:
    integer = parse_int(value)
    if integer >= 1 << 160:
        raise ValueError("address exceeds 160 bits")
    return f"0x{integer:040x}"


def normalize_address(value: str) -> str:
    return address_hex(value).lower()


@dataclass(frozen=True, slots=True)
class StorageMember:
    """A named member inside an in-place composite type."""

    name: str
    slot: int
    offset: int
    type_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slot": self.slot,
            "offset": self.offset,
            "type": self.type_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> StorageMember:
        return cls(
            name=str(value["name"]),
            slot=parse_int(value["slot"]),
            offset=int(value.get("offset", 0)),
            type_id=str(value["type"]),
        )


@dataclass(frozen=True, slots=True)
class StorageType:
    """A normalized compiler storage type."""

    id: str
    label: str
    encoding: Encoding
    number_of_bytes: int
    key: str | None = None
    value: str | None = None
    base: str | None = None
    length: int | None = None
    members: tuple[StorageMember, ...] = ()

    def __post_init__(self) -> None:
        if self.encoding not in {
            "inplace",
            "mapping",
            "dynamic_array",
            "bytes",
            "vyper_dynamic_array",
            "vyper_bytes",
            "opaque",
        }:
            raise ValueError(f"unknown storage encoding for {self.id}: {self.encoding}")
        if self.number_of_bytes < 0:
            raise ValueError(f"negative size for type {self.id}")
        if self.encoding == "mapping" and (self.key is None or self.value is None):
            raise ValueError(f"mapping {self.id} is missing key/value types")
        if self.encoding in {"dynamic_array", "vyper_dynamic_array"} and self.base is None:
            raise ValueError(f"array {self.id} is missing its base type")
        for member in self.members:
            if not 0 <= member.offset < 32:
                raise ValueError(f"invalid byte offset for {self.id}.{member.name}")

    @property
    def words(self) -> int:
        return max(1, (self.number_of_bytes + 31) // 32)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "encoding": self.encoding,
            "number_of_bytes": self.number_of_bytes,
        }
        if self.key is not None:
            out["key"] = self.key
        if self.value is not None:
            out["value"] = self.value
        if self.base is not None:
            out["base"] = self.base
        if self.length is not None:
            out["length"] = self.length
        if self.members:
            out["members"] = [member.to_dict() for member in self.members]
        return out

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> StorageType:
        return cls(
            id=str(value["id"]),
            label=str(value["label"]),
            encoding=str(value["encoding"]),  # type: ignore[arg-type]
            number_of_bytes=int(value["number_of_bytes"]),
            key=str(value["key"]) if value.get("key") is not None else None,
            value=str(value["value"]) if value.get("value") is not None else None,
            base=str(value["base"]) if value.get("base") is not None else None,
            length=int(value["length"]) if value.get("length") is not None else None,
            members=tuple(StorageMember.from_dict(item) for item in value.get("members", [])),
        )


@dataclass(frozen=True, slots=True)
class StorageVariable:
    name: str
    slot: int
    offset: int
    type_id: str
    contract: str | None = None
    ast_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "slot": self.slot,
            "offset": self.offset,
            "type": self.type_id,
        }
        if self.contract is not None:
            out["contract"] = self.contract
        if self.ast_id is not None:
            out["ast_id"] = self.ast_id
        return out

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> StorageVariable:
        return cls(
            name=str(value["name"]),
            slot=parse_int(value["slot"]),
            offset=int(value.get("offset", 0)),
            type_id=str(value["type"]),
            contract=str(value["contract"]) if value.get("contract") is not None else None,
            ast_id=int(value["ast_id"]) if value.get("ast_id") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class StorageLayout:
    """Normalized layout emitted or inferred from an exact compiler."""

    language: Literal["solidity", "vyper"]
    compiler_version: str | None
    contract: str | None
    variables: tuple[StorageVariable, ...]
    types: dict[str, StorageType]
    hash_order: HashOrder
    storage_dialect: StorageDialect
    source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.language not in {"solidity", "vyper"}:
            raise ValueError(f"unsupported layout language: {self.language}")
        if self.hash_order not in {"key-slot", "slot-key"}:
            raise ValueError(f"unsupported mapping hash order: {self.hash_order}")
        if self.storage_dialect not in {
            "solidity",
            "vyper-hashed-composites",
            "vyper-inline",
        }:
            raise ValueError(f"unsupported storage dialect: {self.storage_dialect}")
        if self.language == "solidity" and (
            self.hash_order != "key-slot" or self.storage_dialect != "solidity"
        ):
            raise ValueError("Solidity layouts must use key-slot hashing and Solidity storage")
        if self.language == "vyper" and (
            self.hash_order != "slot-key" or self.storage_dialect == "solidity"
        ):
            raise ValueError("Vyper layouts must use slot-key hashing and a Vyper dialect")
        missing = {variable.type_id for variable in self.variables} - self.types.keys()
        for variable in self.variables:
            if not 0 <= variable.offset < 32:
                raise ValueError(f"invalid byte offset for {variable.name}")
        for type_ in self.types.values():
            refs = {type_.key, type_.value, type_.base} - {None}
            refs.update(member.type_id for member in type_.members)
            missing.update(refs - self.types.keys())
        if missing:
            raise ValueError(f"layout references missing types: {', '.join(sorted(missing))}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "evm-storage/layout/v1",
            "language": self.language,
            "compiler_version": self.compiler_version,
            "contract": self.contract,
            "hash_order": self.hash_order,
            "storage_dialect": self.storage_dialect,
            "source": self.source,
            "variables": [variable.to_dict() for variable in self.variables],
            "types": {key: value.to_dict() for key, value in sorted(self.types.items())},
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> StorageLayout:
        if value.get("schema") not in {None, "evm-storage/layout/v1"}:
            raise ValueError(f"unsupported layout schema: {value.get('schema')}")
        return cls(
            language=str(value["language"]),  # type: ignore[arg-type]
            compiler_version=(
                str(value["compiler_version"])
                if value.get("compiler_version") is not None
                else None
            ),
            contract=str(value["contract"]) if value.get("contract") is not None else None,
            hash_order=str(value["hash_order"]),  # type: ignore[arg-type]
            storage_dialect=str(
                value.get(
                    "storage_dialect",
                    "solidity" if value.get("language") == "solidity" else "vyper-inline",
                )
            ),  # type: ignore[arg-type]
            source=str(value["source"]) if value.get("source") is not None else None,
            variables=tuple(StorageVariable.from_dict(item) for item in value["variables"]),
            types={key: StorageType.from_dict(item) for key, item in value["types"].items()},
            metadata=dict(value.get("metadata", {})),
        )


@dataclass(frozen=True, slots=True)
class Preimage:
    hash: int
    data: bytes
    depth: int | None = None
    code_address: str | None = None
    storage_address: str | None = None

    @property
    def words(self) -> tuple[int, ...]:
        if len(self.data) % 32:
            return ()
        return tuple(
            int.from_bytes(self.data[offset : offset + 32], "big")
            for offset in range(0, len(self.data), 32)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "hash": word_hex(self.hash),
            "data": "0x" + self.data.hex(),
            "depth": self.depth,
            "code_address": self.code_address,
            "storage_address": self.storage_address,
        }


@dataclass(frozen=True, slots=True)
class WriteContext:
    slot: int
    value: int
    depth: int
    code_address: str
    storage_address: str
    pc: int | None = None


@dataclass(frozen=True, slots=True)
class SlotChange:
    address: str
    slot: int
    before: int
    after: int
    write: WriteContext | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "address": self.address,
            "slot": word_hex(self.slot),
            "before": word_hex(self.before),
            "after": word_hex(self.after),
        }
        if self.write is not None:
            out["code_address"] = self.write.code_address
            out["storage_address"] = self.write.storage_address
            out["depth"] = self.write.depth
            out["pc"] = self.write.pc
        return out


@dataclass(frozen=True, slots=True)
class ResolvedChange:
    change: SlotChange
    path: str | None
    type_label: str | None
    before: Any
    after: Any
    confidence: Literal["exact", "partial", "unresolved"]
    reason: str | None = None
    layout_address: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = self.change.to_dict()
        out.update(
            {
                "path": self.path,
                "type": self.type_label,
                "decoded_before": self.before,
                "decoded_after": self.after,
                "confidence": self.confidence,
            }
        )
        if self.reason is not None:
            out["reason"] = self.reason
        if self.layout_address is not None:
            out["layout_address"] = self.layout_address
        return out
