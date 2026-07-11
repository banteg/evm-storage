import json
from dataclasses import replace

import pytest
from eth_hash.auto import keccak

import evm_storage.cli as cli
from evm_storage.cli import _normalize_block_tag, main
from evm_storage.errors import LayoutError
from evm_storage.model import SlotChange
from evm_storage.trace import TraceBundle


def test_normalize_cli(tmp_path, solidity_artifact, capsys):
    artifact = tmp_path / "artifact.json"
    artifact.write_text(json.dumps(solidity_artifact))
    assert main(["layout", "normalize", str(artifact), "--language", "solidity"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["schema"] == "evm-storage/layout/v1"
    assert output["language"] == "solidity"


def test_snapshot_diff_cli(tmp_path, capsys):
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    before.write_text(
        json.dumps(
            {
                "schema": "evm-storage/snapshot/v1",
                "address": "0x" + "11" * 20,
                "block": 1,
                "values": {"x": 1, "y": 2},
            }
        )
    )
    after.write_text(
        json.dumps(
            {
                "schema": "evm-storage/snapshot/v1",
                "address": "0x" + "11" * 20,
                "block": 2,
                "values": {"x": 3, "y": 2},
            }
        )
    )
    assert main(["diff", str(before), str(after), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["changes"] == [{"after": 3, "before": 1, "path": "x"}]


def test_decimal_block_is_normalized_to_rpc_quantity():
    assert _normalize_block_tag("22000000") == "0x14fb180"
    assert _normalize_block_tag("0x10") == "0x10"
    assert _normalize_block_tag("latest") == "latest"


def test_snapshot_pins_latest_to_one_block(tmp_path, solidity_layout, monkeypatch, capsys):
    paths = tmp_path / "paths.txt"
    paths.write_text("small\n")

    class FakeRPC:
        def __init__(self):
            self.storage_blocks = []

        def chain_id(self):
            return 1

        def block(self, block):
            assert block in {"latest", "0x10"}
            return {"number": "0x10", "hash": "0x" + "ab" * 32}

        def storage(self, _address, _slot, block):
            self.storage_blocks.append(block)
            return 7

    rpc = FakeRPC()
    monkeypatch.setattr(cli, "_rpc", lambda _args: rpc)
    monkeypatch.setattr(cli, "load_layout_file", lambda *_args, **_kwargs: solidity_layout)
    assert (
        main(
            [
                "snapshot",
                "0x" + "11" * 20,
                "--paths",
                str(paths),
                "--layout",
                "unused.json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["block"] == "0x10"
    assert output["block_hash"] == "0x" + "ab" * 32
    assert output["chain_id"] == 1
    assert rpc.storage_blocks == ["0x10"]


def test_snapshot_diff_rejects_different_addresses(tmp_path, capsys):
    snapshots = []
    for index, address in enumerate(("11", "22")):
        path = tmp_path / f"snapshot-{index}.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "evm-storage/snapshot/v1",
                    "address": "0x" + address * 20,
                    "chain_id": 1,
                    "block": index,
                    "values": {"x": index},
                }
            )
        )
        snapshots.append(path)
    assert main(["diff", str(snapshots[0]), str(snapshots[1]), "--json"]) == 2
    assert "different contract addresses" in capsys.readouterr().err


def test_no_auto_layout_still_aliases_supplied_implementation(
    tmp_path, solidity_artifact, monkeypatch, capsys
):
    proxy = "0x" + "11" * 20
    implementation = "0x" + "22" * 20
    artifact = tmp_path / "layout.json"
    artifact.write_text(json.dumps(solidity_artifact))
    bundle = TraceBundle(
        tx_hash="0x" + "33" * 32,
        transaction={"blockNumber": "0x10"},
        receipt={},
        changes=(SlotChange(proxy, 0, 1, 2),),
        preimages=(),
        writes=(),
        client_version="fake",
    )

    class FakeRPC:
        def code(self, _address, _block):
            return "0x363d3d373d3d3d363d73" + implementation[2:] + "5af43d82803e903d91602b57fd5bf3"

    class FakeProvider:
        def __init__(self, _rpc):
            pass

        def transaction(self, _tx_hash):
            return bundle

    monkeypatch.setattr(cli, "_rpc", lambda _args: FakeRPC())
    monkeypatch.setattr(cli, "TraceProvider", FakeProvider)
    monkeypatch.setattr(
        cli,
        "fetch_sourcify_layout",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("remote fetch")),
    )
    assert (
        main(
            [
                "tx",
                bundle.tx_hash,
                "--no-auto-layout",
                "--layout",
                f"{implementation}={artifact}",
                "--json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["changes"][0]["path"] == "small"
    assert output["changes"][0]["confidence"] == "partial"
    assert output["layouts"][proxy]["metadata"]["attribution"] == "static-proxy"


def test_sourcify_layout_is_bound_to_historical_runtime_code(solidity_layout, monkeypatch):
    address = "0x" + "11" * 20
    code = "0x6000"
    expected = "0x" + keccak(bytes.fromhex("6000")).hex()
    remote = replace(
        solidity_layout,
        metadata={**solidity_layout.metadata, "sourcify": {"runtime_code_hash": expected}},
    )

    class FakeRPC:
        def code(self, _address, _block):
            return code

    monkeypatch.setattr(cli, "fetch_sourcify_layout", lambda *_args, **_kwargs: remote)
    result = cli._fetch_sourcify_for_block(
        FakeRPC(),
        1,
        address,
        "0x10",
        base_url="https://sourcify.invalid",
    )
    assert result.metadata["sourcify"]["code_validation"] == "matched"
    assert result.metadata["sourcify"]["validated_block"] == "0x10"


def test_sourcify_layout_is_rejected_on_historical_code_mismatch(solidity_layout, monkeypatch):
    address = "0x" + "11" * 20
    remote = replace(
        solidity_layout,
        metadata={
            **solidity_layout.metadata,
            "sourcify": {"runtime_code_hash": "0x" + "00" * 32},
        },
    )

    class FakeRPC:
        def code(self, _address, _block):
            return "0x6000"

    monkeypatch.setattr(cli, "fetch_sourcify_layout", lambda *_args, **_kwargs: remote)
    with pytest.raises(LayoutError, match="layout rejected"):
        cli._fetch_sourcify_for_block(
            FakeRPC(),
            1,
            address,
            "0x10",
            base_url="https://sourcify.invalid",
        )
