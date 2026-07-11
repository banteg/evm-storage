from eth_hash.auto import keccak

from evm_storage.layout import load_layout
from evm_storage.model import Preimage, SlotChange, WriteContext
from evm_storage.resolver import StorageResolver, resolve_bundle
from evm_storage.trace import TraceBundle


def test_resolves_only_changed_packed_field(solidity_layout):
    before = 1 | (2 << 128)
    after = 3 | (2 << 128)
    result = StorageResolver(solidity_layout).resolve(
        SlotChange("0x" + "11" * 20, 0, before, after)
    )
    assert len(result) == 1
    assert result[0].path == "small"
    assert result[0].before == 1
    assert result[0].after == 3


def test_resolves_solidity_mapping(solidity_layout):
    address = int("1234567890abcdef1234567890abcdef12345678", 16)
    data = address.to_bytes(32, "big") + (5).to_bytes(32, "big")
    digest = int.from_bytes(keccak(data), "big")
    preimage = Preimage(digest, data, storage_address="0x" + "11" * 20)
    result = StorageResolver(solidity_layout, [preimage]).resolve(
        SlotChange("0x" + "11" * 20, digest, 7, 9)
    )
    assert result[0].path == "balances[0x1234567890abcdef1234567890abcdef12345678]"
    assert result[0].before == 7
    assert result[0].after == 9


def test_resolves_solidity_dynamic_array(solidity_layout):
    data = (6).to_bytes(32, "big")
    base = int.from_bytes(keccak(data), "big")
    result = StorageResolver(solidity_layout, [Preimage(base, data)]).resolve(
        SlotChange("0x" + "11" * 20, base + 3, 10, 11)
    )
    assert result[0].path == "items[3]"
    assert result[0].confidence == "partial"


def test_dynamic_array_candidate_does_not_override_exact_mapping(solidity_layout):
    array_data = (6).to_bytes(32, "big")
    array_base = int.from_bytes(keccak(array_data), "big")
    for key in range(1, 100):
        mapping_data = key.to_bytes(32, "big") + (5).to_bytes(32, "big")
        target = int.from_bytes(keccak(mapping_data), "big")
        if target >= array_base:
            break
    else:  # pragma: no cover - overwhelmingly impossible
        raise AssertionError("could not find deterministic mapping fixture above array root")
    result = StorageResolver(
        solidity_layout,
        [Preimage(array_base, array_data), Preimage(target, mapping_data)],
    ).resolve(SlotChange("0x" + "11" * 20, target, 1, 2))
    assert len(result) == 1
    assert result[0].path == f"balances[0x{key:040x}]"
    assert result[0].confidence == "exact"


def test_vyper_uses_slot_key_mapping_order():
    layout = load_layout(
        {
            "compiler": "vyper-0.4.3",
            "layout": {"m": {"type": "HashMap[address, uint256]", "slot": 1, "n_slots": 1}},
        },
        language="vyper",
    )
    key = int("22" * 20, 16)
    data = (1).to_bytes(32, "big") + key.to_bytes(32, "big")
    digest = int.from_bytes(keccak(data), "big")
    result = StorageResolver(layout, [Preimage(digest, data)]).resolve(
        SlotChange("0x" + "11" * 20, digest, 1, 2)
    )
    assert result[0].path == f"m[0x{'22' * 20}]"


def test_resolves_solidity_string_mapping_preimage():
    layout = load_layout(
        {
            "storage": [{"label": "values", "slot": "2", "offset": 0, "type": "t_map"}],
            "types": {
                "t_string": {
                    "encoding": "bytes",
                    "label": "string",
                    "numberOfBytes": "32",
                },
                "t_uint": {
                    "encoding": "inplace",
                    "label": "uint256",
                    "numberOfBytes": "32",
                },
                "t_map": {
                    "encoding": "mapping",
                    "label": "mapping(string => uint256)",
                    "numberOfBytes": "32",
                    "key": "t_string",
                    "value": "t_uint",
                },
            },
        },
        language="solidity",
    )
    data = b"hello" + (2).to_bytes(32, "big")
    digest = int.from_bytes(keccak(data), "big")
    result = StorageResolver(layout, [Preimage(digest, data)]).resolve(
        SlotChange("0x" + "11" * 20, digest, 1, 2)
    )
    assert result[0].path == "values['hello']"


def test_legacy_vyper_hashes_mapping_value_struct_root():
    layout = load_layout(
        {
            "schema": "evm-storage/vyper-worker/v1",
            "compiler_version": "0.2.12",
            "layout": {"records": {"type": "HashMap[address, S]", "slot": 2, "n_slots": 1}},
            "type_definitions": {
                "S": {
                    "members": [{"name": "amount", "type": "uint256", "slot": 0, "n_slots": 1}],
                    "n_slots": 1,
                }
            },
            "extraction": {"method": "global-context", "storage_dialect": "legacy-hashed"},
        },
        language="vyper",
    )
    key = int("33" * 20, 16)
    mapping_data = (2).to_bytes(32, "big") + key.to_bytes(32, "big")
    entry = int.from_bytes(keccak(mapping_data), "big")
    struct_root = int.from_bytes(keccak(entry.to_bytes(32, "big")), "big")
    result = StorageResolver(layout, [Preimage(entry, mapping_data)]).resolve(
        SlotChange("0x" + "11" * 20, struct_root, 4, 5)
    )
    assert result[0].path == f"records[0x{'33' * 20}].amount"


def test_legacy_vyper_resolves_nested_array_root_preimage():
    layout = load_layout(
        {
            "schema": "evm-storage/vyper-worker/v1",
            "compiler_version": "0.2.12",
            "layout": {"values": {"type": "uint256[2][2]", "slot": 0, "n_slots": 1}},
            "extraction": {"method": "global-context", "storage_dialect": "legacy-hashed"},
        },
        language="vyper",
    )
    root_data = (0).to_bytes(32, "big")
    outer_root = int.from_bytes(keccak(root_data), "big")
    child_data = (outer_root + 1).to_bytes(32, "big")
    child_root = int.from_bytes(keccak(child_data), "big")
    result = StorageResolver(
        layout,
        [Preimage(outer_root, root_data), Preimage(child_root, child_data)],
    ).resolve(SlotChange("0x" + "11" * 20, child_root, 1, 2))
    assert result[0].path == "values[1][0]"


def test_decodes_vyper_sign_extended_and_left_aligned_scalars():
    layout = load_layout(
        {
            "compiler": "vyper-0.4.3",
            "layout": {
                "signed": {"type": "int128", "slot": 0, "n_slots": 1},
                "decimal_value": {"type": "decimal", "slot": 1, "n_slots": 1},
                "fixed": {"type": "bytes4", "slot": 2, "n_slots": 1},
            },
        },
        language="vyper",
    )
    address = "0x" + "11" * 20
    minus_one = (1 << 256) - 1
    signed = StorageResolver(layout).resolve(SlotChange(address, 0, 0, minus_one))
    decimal = StorageResolver(layout).resolve(
        SlotChange(address, 1, 0, (-10_000_000_001) & ((1 << 256) - 1))
    )
    fixed = StorageResolver(layout).resolve(
        SlotChange(address, 2, 0, int.from_bytes(bytes.fromhex("11223344") + bytes(28), "big"))
    )
    assert signed[0].after == -1
    assert decimal[0].after == "-1.0000000001"
    assert fixed[0].after == "0x11223344"


def test_legacy_vyper_bytes_does_not_claim_allocator_gap():
    layout = load_layout(
        {
            "schema": "evm-storage/vyper-worker/v1",
            "compiler_version": "0.2.12",
            "layout": {"data": {"type": "Bytes[65]", "slot": 0, "n_slots": 1}},
            "extraction": {"method": "global-context", "storage_dialect": "legacy-hashed"},
        },
        language="vyper",
    )
    root_data = (0).to_bytes(32, "big")
    root = int.from_bytes(keccak(root_data), "big")
    result = StorageResolver(layout, [Preimage(root, root_data)]).resolve(
        SlotChange("0x" + "11" * 20, root + 4, 1, 2)
    )
    assert result[0].confidence == "unresolved"


def test_resolves_packed_fixed_array_element():
    layout = load_layout(
        {
            "storage": [{"label": "flags", "slot": "0", "offset": 0, "type": "t_array"}],
            "types": {
                "t_uint8": {
                    "encoding": "inplace",
                    "label": "uint8",
                    "numberOfBytes": "1",
                },
                "t_array": {
                    "encoding": "inplace",
                    "label": "uint8[4]",
                    "numberOfBytes": "32",
                    "base": "t_uint8",
                },
            },
        },
        language="solidity",
    )
    result = StorageResolver(layout).resolve(SlotChange("0x" + "11" * 20, 0, 1 << 16, 7 << 16))
    assert [(item.path, item.before, item.after) for item in result] == [("flags[2]", 1, 7)]


def test_long_bytes_does_not_claim_unobserved_high_entropy_slot():
    layout = load_layout(
        {
            "storage": [{"label": "name", "slot": "0", "offset": 0, "type": "t_string"}],
            "types": {
                "t_string": {
                    "encoding": "bytes",
                    "label": "string",
                    "numberOfBytes": "32",
                }
            },
        },
        language="solidity",
    )
    result = StorageResolver(layout).resolve(SlotChange("0x" + "11" * 20, (1 << 255) + 123, 1, 2))
    assert result[0].confidence == "unresolved"


def test_decodes_solidity_short_string_head():
    layout = load_layout(
        {
            "storage": [{"label": "name", "slot": "0", "offset": 0, "type": "t_string"}],
            "types": {
                "t_string": {
                    "encoding": "bytes",
                    "label": "string",
                    "numberOfBytes": "32",
                }
            },
        },
        language="solidity",
    )

    def encoded(value: str) -> int:
        data = value.encode()
        return int.from_bytes(data.ljust(31, b"\x00") + bytes([len(data) * 2]), "big")

    result = StorageResolver(layout).resolve(
        SlotChange("0x" + "11" * 20, 0, encoded("old"), encoded("new"))
    )
    assert result[0].before == "old"
    assert result[0].after == "new"


def test_does_not_apply_a_layout_to_an_unrelated_address(solidity_layout):
    change = SlotChange("0x" + "22" * 20, 0, 1, 2)
    result = resolve_bundle(
        _bundle(change),
        {"0x" + "11" * 20: solidity_layout},
    )
    assert result[0].confidence == "unresolved"
    assert result[0].layout_address is None


def test_falls_back_from_code_layout_to_storage_layout(solidity_layout):
    code_address = "0x" + "11" * 20
    storage_address = "0x" + "22" * 20
    storage_layout = load_layout(
        {
            "storage": [{"label": "value", "slot": "42", "offset": 0, "type": "t_uint"}],
            "types": {
                "t_uint": {
                    "encoding": "inplace",
                    "label": "uint256",
                    "numberOfBytes": "32",
                }
            },
        },
        language="solidity",
    )
    write = WriteContext(42, 2, 1, code_address, storage_address)
    change = SlotChange(storage_address, 42, 1, 2, write)
    result = resolve_bundle(
        _bundle(change),
        {code_address: solidity_layout, storage_address: storage_layout},
    )
    assert result[0].path == "value"
    assert result[0].layout_address == storage_address


def _bundle(*changes: SlotChange) -> TraceBundle:
    return TraceBundle(
        tx_hash="0x" + "00" * 32,
        transaction={},
        receipt={},
        changes=changes,
        preimages=(),
        writes=(),
        client_version=None,
    )
