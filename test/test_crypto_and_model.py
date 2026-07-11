import pytest
from eth_hash.auto import keccak

from evm_storage.model import StorageLayout, address_hex, parse_int, word_hex


def test_ethereum_keccak_vector():
    assert keccak(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"


def test_word_and_address_normalization():
    assert parse_int("0xff") == 255
    assert parse_int("255") == 255
    assert word_hex(1).endswith("0001")
    assert address_hex(1) == "0x0000000000000000000000000000000000000001"


def test_normalized_layout_round_trip(solidity_layout):
    restored = StorageLayout.from_dict(solidity_layout.to_dict())
    assert restored == solidity_layout
    assert restored.storage_dialect == "solidity"


def test_normalized_layout_rejects_inconsistent_hash_dialect(solidity_layout):
    value = solidity_layout.to_dict()
    value["hash_order"] = "slot-key"
    with pytest.raises(ValueError, match="Solidity layouts must use key-slot"):
        StorageLayout.from_dict(value)
