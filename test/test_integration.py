from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from eth_hash.auto import keccak

from evm_storage.compiler import extract_solidity, extract_vyper
from evm_storage.paths import locate
from evm_storage.rpc import RPCClient
from evm_storage.trace import TraceProvider


@pytest.mark.compiler
@pytest.mark.skipif(
    os.getenv("EVM_STORAGE_COMPILER_TESTS") != "1",
    reason="set EVM_STORAGE_COMPILER_TESTS=1 to install exact compiler fixtures",
)
@pytest.mark.parametrize(
    ("version", "dialect"),
    [
        ("0.1.0b17", "vyper-hashed-composites"),
        ("0.2.12", "vyper-hashed-composites"),
        ("0.2.15", "vyper-inline"),
        ("0.2.16", "vyper-inline"),
        ("0.3.2", "vyper-inline"),
        ("0.4.3", "vyper-inline"),
    ],
)
def test_exact_vyper_worker_matrix(tmp_path, version, dialect):
    source = tmp_path / "Fixture.vy"
    mapping = "map(address, uint256)" if version == "0.1.0b17" else "HashMap[address, uint256]"
    source.write_text(f"owner: address\nbalances: {mapping}\nvalues: uint256[3]\n")
    layout = extract_vyper(source, version=version)
    assert layout.storage_dialect == dialect
    assert {item.name: item.slot for item in layout.variables}["balances"] == 1
    mapping_type = next(item for item in layout.types.values() if item.encoding == "mapping")
    assert layout.types[mapping_type.key].label == "address"
    assert layout.types[mapping_type.value].label == "uint256"


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("EVM_STORAGE_LIVE_TESTS") != "1",
    reason="set EVM_STORAGE_LIVE_TESTS=1 with a mainnet archive RPC",
)
def test_live_reth_usdc_delegatecall():
    rpc = RPCClient(os.getenv("EVM_STORAGE_RPC_URL", "http://127.0.0.1:8545"), timeout=300)
    bundle = TraceProvider(rpc).transaction(
        "0xe4e6a54918fcf739ef6a2443b990d53607c87ca3d88091b91736ce96ce6f04e3"
    )
    assert len(bundle.changes) == 2
    assert {change.write.code_address for change in bundle.changes} == {
        "0x43506849d7c04f9138d1a2050bbf3a0c054402dd"
    }
    assert {change.address for change in bundle.changes} == {
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    }


@pytest.mark.compiler
@pytest.mark.skipif(
    os.getenv("EVM_STORAGE_COMPILER_TESTS") != "1",
    reason="set EVM_STORAGE_COMPILER_TESTS=1 to install exact compiler fixtures",
)
def test_vyper_cli_fallback_resolves_explicit_import_path():
    root = Path(__file__).parent / "fixtures" / "vyper_project"
    layout = extract_vyper(
        root / "contracts" / "Imported.vy",
        version="0.4.3",
        paths=[root],
    )
    assert [(variable.name, variable.slot) for variable in layout.variables] == [("owner", 0)]
    assert layout.metadata["extraction"]["method"] == "native-cli-fallback"


@pytest.mark.compiler
@pytest.mark.skipif(
    os.getenv("EVM_STORAGE_COMPILER_TESTS") != "1" or shutil.which("solc") is None,
    reason="set EVM_STORAGE_COMPILER_TESTS=1 and install solc",
)
def test_local_solc_emits_storage_layout(tmp_path):
    source = tmp_path / "Fixture.sol"
    source.write_text(
        "pragma solidity >=0.5.13;"
        "contract Fixture { uint128 a; uint128 b; mapping(address => uint256) balances; }"
    )
    layout = extract_solidity(source)
    variables = {variable.name: variable for variable in layout.variables}
    assert (variables["a"].slot, variables["a"].offset) == (0, 0)
    assert (variables["b"].slot, variables["b"].offset) == (0, 16)
    assert variables["balances"].slot == 1


@pytest.mark.compiler
@pytest.mark.skipif(
    os.getenv("EVM_STORAGE_COMPILER_TESTS") != "1",
    reason="set EVM_STORAGE_COMPILER_TESTS=1 to install exact compiler fixtures",
)
def test_legacy_vyper_worker_preserves_composite_logical_spans():
    source = Path(__file__).parent / "fixtures" / "vyper_legacy_composites.vy"
    layout = extract_vyper(source, version="0.2.12")
    variables = {variable.name: variable for variable in layout.variables}
    assert layout.types[variables["data"].type_id].words == 4
    outer_root = int.from_bytes(keccak((0).to_bytes(32, "big")), "big")
    expected = int.from_bytes(keccak((outer_root + 1).to_bytes(32, "big")), "big")
    assert locate(layout, "values[1][0]").slot == expected
