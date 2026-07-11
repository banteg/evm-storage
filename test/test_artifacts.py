from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest
from eth_hash.auto import keccak

from evm_storage.artifacts import fetch_sourcify_layout
from evm_storage.errors import LayoutError


def _response(value: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(value).encode())


def test_fetches_sourcify_solidity_layout_with_compilation_provenance():
    response = {
        "storageLayout": {
            "storage": [
                {
                    "astId": 1,
                    "contract": "Contract.sol:Contract",
                    "label": "owner",
                    "offset": 0,
                    "slot": "0",
                    "type": "t_address",
                }
            ],
            "types": {
                "t_address": {
                    "encoding": "inplace",
                    "label": "address",
                    "numberOfBytes": "20",
                }
            },
        },
        "matchId": "42",
        "match": "match",
        "runtimeMatch": "match",
        "runtimeBytecode": {"onchainBytecode": "0x6000"},
        "deployment": {"blockNumber": "100", "transactionHash": "0x" + "ab" * 32},
        "compilation": {"language": "Solidity", "compilerVersion": "0.8.28"},
        "chainId": "1",
        "address": "0x0000000000000000000000000000000000000001",
    }
    with patch("urllib.request.urlopen", return_value=_response(response)) as urlopen:
        layout = fetch_sourcify_layout(1, response["address"])

    request = urlopen.call_args.args[0]
    assert request.full_url.endswith("?fields=all")
    assert layout.language == "solidity"
    assert layout.compiler_version == "0.8.28"
    assert layout.variables[0].name == "owner"
    assert layout.metadata["sourcify"]["matchId"] == "42"
    assert (
        layout.metadata["sourcify"]["runtime_code_hash"]
        == "0x" + keccak(bytes.fromhex("6000")).hex()
    )
    assert layout.metadata["sourcify"]["deployment"]["blockNumber"] == "100"


def test_infers_vyper_from_storage_layout_shape():
    response = {
        "storageLayout": {
            "receiver": {"slot": 3, "type": "address", "n_slots": 1},
            "balances": {
                "slot": 0,
                "type": "HashMap[address, uint256]",
                "n_slots": 1,
            },
        },
        "compilation": {"language": "Vyper", "compilerVersion": "0.4.3"},
        "match": "match",
        "chainId": "1",
        "address": "0x0000000000000000000000000000000000000002",
    }
    with patch("urllib.request.urlopen", return_value=_response(response)):
        layout = fetch_sourcify_layout(1, response["address"])

    assert layout.language == "vyper"
    assert layout.compiler_version == "0.4.3"
    assert layout.hash_order == "slot-key"
    assert layout.variables[0].name == "balances"
    assert layout.variables[1].name == "receiver"


def test_reports_sourcify_match_without_storage_layout():
    response = {
        "match": "match",
        "chainId": "1",
        "address": "0x0000000000000000000000000000000000000003",
    }
    with (
        patch("urllib.request.urlopen", return_value=_response(response)),
        pytest.raises(LayoutError, match="has no storage layout"),
    ):
        fetch_sourcify_layout(1, response["address"])


def test_reports_unverified_sourcify_contract():
    response = {
        "match": None,
        "chainId": "1",
        "address": "0x0000000000000000000000000000000000000004",
    }
    with (
        patch("urllib.request.urlopen", return_value=_response(response)),
        pytest.raises(LayoutError, match="no Sourcify match"),
    ):
        fetch_sourcify_layout(1, response["address"])


def test_limits_sourcify_response_size():
    address = "0x0000000000000000000000000000000000000005"
    with (
        patch("urllib.request.urlopen", return_value=_response({"storageLayout": {}})),
        pytest.raises(LayoutError, match="response exceeds explicit 8-byte limit"),
    ):
        fetch_sourcify_layout(1, address, max_response_bytes=8)
