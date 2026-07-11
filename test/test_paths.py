import pytest
from eth_hash.auto import keccak

from evm_storage.errors import LayoutError
from evm_storage.layout import load_layout
from evm_storage.paths import decode_location, locate


def test_locates_solidity_mapping_path(solidity_layout):
    key = "0x1234567890abcdef1234567890abcdef12345678"
    location = locate(solidity_layout, f"balances[{key}]")
    expected = keccak(int(key, 16).to_bytes(32, "big") + (5).to_bytes(32, "big"))
    assert location.slot == int.from_bytes(expected, "big")


def test_locates_and_decodes_packed_value(solidity_layout):
    location = locate(solidity_layout, "other")
    assert location.slot == 0
    assert location.offset == 16
    assert decode_location(solidity_layout, location, 7 << 128) == 7


def test_locates_vyper_mapping_in_opposite_order():
    layout = load_layout(
        {"layout": {"m": {"type": "HashMap[uint256, uint256]", "slot": 4, "n_slots": 1}}},
        language="vyper",
        compiler_version="0.4.3",
    )
    location = locate(layout, "m[9]")
    expected = keccak((4).to_bytes(32, "big") + (9).to_bytes(32, "big"))
    assert location.slot == int.from_bytes(expected, "big")


def test_known_mapping_hash_order_vectors():
    key = (0x1234).to_bytes(32, "big")
    slot = (1).to_bytes(32, "big")
    assert (
        keccak(slot + key).hex()
        == "c30415b421fdb672017db1512ca381cf086c7d69add633736ec72339eaf2f162"
    )
    assert (
        keccak(key + slot).hex()
        == "63b939821a5be8a0d41f2b7a5fc118fa09d99968c6010c590a5daf26b37cd05d"
    )


def test_solidity_string_mapping_key_is_hashed_unpadded():
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
    location = locate(layout, 'values["hello"]')
    expected = keccak(b"hello" + (2).to_bytes(32, "big"))
    assert location.slot == int.from_bytes(expected, "big")


def test_solidity_signed_mapping_key_is_sign_extended():
    layout = load_layout(
        {
            "storage": [{"label": "values", "slot": "2", "offset": 0, "type": "t_map"}],
            "types": {
                "t_int8": {
                    "encoding": "inplace",
                    "label": "int8",
                    "numberOfBytes": "1",
                },
                "t_uint": {
                    "encoding": "inplace",
                    "label": "uint256",
                    "numberOfBytes": "32",
                },
                "t_map": {
                    "encoding": "mapping",
                    "label": "mapping(int8 => uint256)",
                    "numberOfBytes": "32",
                    "key": "t_int8",
                    "value": "t_uint",
                },
            },
        },
        language="solidity",
    )
    location = locate(layout, "values[-1]")
    expected = keccak(b"\xff" * 32 + (2).to_bytes(32, "big"))
    assert location.slot == int.from_bytes(expected, "big")
    with pytest.raises(LayoutError, match="outside int8"):
        locate(layout, "values[-129]")


def test_legacy_vyper_nested_array_uses_one_root_slot_per_element():
    layout = load_layout(
        {
            "schema": "evm-storage/vyper-worker/v1",
            "compiler_version": "0.2.12",
            "layout": {"values": {"type": "uint256[2][2]", "slot": 0, "n_slots": 1}},
            "extraction": {"method": "global-context", "storage_dialect": "legacy-hashed"},
        },
        language="vyper",
    )
    location = locate(layout, "values[1][0]")
    outer_root = int.from_bytes(keccak((0).to_bytes(32, "big")), "big")
    expected = keccak((outer_root + 1).to_bytes(32, "big"))
    assert location.slot == int.from_bytes(expected, "big")
