import pytest

from evm_storage.errors import LayoutError
from evm_storage.layout import load_layout


def test_normalizes_solidity_layout(solidity_layout):
    assert solidity_layout.language == "solidity"
    assert solidity_layout.compiler_version == "0.8.29+commit.ab55807c"
    assert solidity_layout.hash_order == "key-slot"
    assert solidity_layout.types["t_mapping"].key == "t_address"
    assert solidity_layout.variables[1].offset == 16


def test_selects_contract_from_standard_json(solidity_artifact):
    output = {"contracts": {"A.sol": {"A": solidity_artifact}}}
    layout = load_layout(output, language="solidity", contract="A")
    assert layout.contract == "A.sol:A"


def test_ambiguous_contract_fails(solidity_artifact):
    output = {
        "contracts": {
            "A.sol": {"C": solidity_artifact},
            "B.sol": {"C": solidity_artifact},
        }
    }
    with pytest.raises(LayoutError, match="ambiguous"):
        load_layout(output, language="solidity", contract="C")


def test_empty_solidity_layout_allows_null_types():
    layout = load_layout({"storage": [], "types": None}, language="solidity")
    assert layout.variables == ()
    assert layout.types == {}


def test_ast_enriches_true_declaring_contract():
    artifact = {
        "storage": [
            {
                "astId": 7,
                "contract": "Derived.sol:Derived",
                "label": "baseValue",
                "slot": "0",
                "offset": 0,
                "type": "t_uint256",
            }
        ],
        "types": {
            "t_uint256": {
                "encoding": "inplace",
                "label": "uint256",
                "numberOfBytes": "32",
            }
        },
        "sources": {
            "Base.sol": {
                "ast": {
                    "nodeType": "SourceUnit",
                    "nodes": [
                        {
                            "id": 3,
                            "nodeType": "ContractDefinition",
                            "name": "Base",
                            "nodes": [
                                {
                                    "id": 7,
                                    "nodeType": "VariableDeclaration",
                                    "name": "baseValue",
                                    "stateVariable": True,
                                }
                            ],
                        }
                    ],
                }
            }
        },
    }
    layout = load_layout(artifact, language="solidity")
    assert layout.variables[0].contract == "Base.sol:Base"
