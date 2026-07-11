from __future__ import annotations

import pytest

from evm_storage.layout import load_layout


@pytest.fixture
def solidity_artifact():
    return {
        "compiler": {"version": "0.8.29+commit.ab55807c"},
        "storageLayout": {
            "storage": [
                {"label": "small", "slot": "0", "offset": 0, "type": "t_uint128"},
                {"label": "other", "slot": "0", "offset": 16, "type": "t_uint128"},
                {"label": "owner", "slot": "1", "offset": 0, "type": "t_address"},
                {"label": "balances", "slot": "5", "offset": 0, "type": "t_mapping"},
                {"label": "items", "slot": "6", "offset": 0, "type": "t_dynarray"},
            ],
            "types": {
                "t_uint128": {
                    "encoding": "inplace",
                    "label": "uint128",
                    "numberOfBytes": "16",
                },
                "t_uint256": {
                    "encoding": "inplace",
                    "label": "uint256",
                    "numberOfBytes": "32",
                },
                "t_address": {
                    "encoding": "inplace",
                    "label": "address",
                    "numberOfBytes": "20",
                },
                "t_mapping": {
                    "encoding": "mapping",
                    "label": "mapping(address => uint256)",
                    "numberOfBytes": "32",
                    "key": "t_address",
                    "value": "t_uint256",
                },
                "t_dynarray": {
                    "encoding": "dynamic_array",
                    "label": "uint256[]",
                    "numberOfBytes": "32",
                    "base": "t_uint256",
                },
            },
        },
    }


@pytest.fixture
def solidity_layout(solidity_artifact):
    return load_layout(solidity_artifact, language="solidity")
