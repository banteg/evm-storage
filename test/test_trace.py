from eth_hash.auto import keccak

from evm_storage.errors import RPCError
from evm_storage.model import normalize_address
from evm_storage.trace import TraceProvider, analyze_struct_logs, parse_prestate_diff


def test_prestate_diff_insert_update_and_delete():
    result = parse_prestate_diff(
        {
            "pre": {"0x" + "11" * 20: {"storage": {"0x01": "0x02", "0x03": "0x04"}}},
            "post": {"0x" + "11" * 20: {"storage": {"0x01": "0x05", "0x06": "0x07"}}},
        }
    )
    assert [(item.slot, item.before, item.after) for item in result] == [
        (1, 2, 5),
        (3, 4, 0),
        (6, 0, 7),
    ]


def test_reth_word_array_struct_log_extracts_preimage():
    data = (1).to_bytes(32, "big") + (2).to_bytes(32, "big")
    result = analyze_struct_logs(
        [
            {
                "pc": 1,
                "op": "KECCAK256",
                "depth": 1,
                "stack": ["0x40", "0x00"],
                "memory": ["0x" + data[:32].hex(), "0x" + data[32:].hex()],
            }
        ],
        root_address="0x" + "11" * 20,
    )
    assert result.preimages[0].data == data
    assert result.preimages[0].hash == int.from_bytes(keccak(data), "big")


def test_geth_new_struct_log_shape_extracts_preimage():
    data = b"hello".ljust(64, b"\x00")
    result = analyze_struct_logs(
        [
            {
                "pc": 1,
                "op": 0x20,
                "opName": "KECCAK256",
                "depth": 1,
                "stack": ["0x40", "0x0"],
                "memory": "0x" + data.hex(),
            }
        ],
        root_address="0x" + "11" * 20,
    )
    assert result.preimages[0].data == data


def test_skips_impossibly_large_keccak_preimage():
    result = analyze_struct_logs(
        [
            {
                "pc": 1,
                "op": "KECCAK256",
                "depth": 1,
                "stack": [hex(1 << 255), "0x0"],
                "memory": [],
            }
        ],
        root_address="0x" + "11" * 20,
    )
    assert not result.preimages
    assert "preimage limit" in result.warnings[0]


def test_delegatecall_tracks_code_and_storage_context():
    proxy = "0x" + "11" * 20
    implementation = "0x" + "22" * 20
    stack = ["0x0", implementation, "0xffff"]
    logs = [
        {"pc": 1, "op": "DELEGATECALL", "depth": 1, "stack": stack, "memory": []},
        {
            "pc": 2,
            "op": "SSTORE",
            "depth": 2,
            "stack": ["0x07", "0x05"],
            "memory": [],
        },
        {"pc": 3, "op": "STOP", "depth": 1, "stack": [], "memory": []},
    ]
    result = analyze_struct_logs(logs, root_address=proxy)
    write = result.writes[0]
    assert write.storage_address == normalize_address(proxy)
    assert write.code_address == normalize_address(implementation)
    assert (write.slot, write.value) == (5, 7)


def test_root_code_resolver_models_eip7702_code_storage_split():
    authority = "0x" + "44" * 20
    delegation = "0x" + "55" * 20
    result = analyze_struct_logs(
        [
            {
                "pc": 1,
                "op": "SSTORE",
                "depth": 1,
                "stack": ["0x02", "0x01"],
                "memory": [],
            }
        ],
        root_address=authority,
        resolve_code=lambda _address: delegation,
    )
    assert result.writes[0].code_address == delegation
    assert result.writes[0].storage_address == authority


def test_trace_does_not_choose_between_ambiguous_final_writers():
    proxy = "0x" + "11" * 20
    implementations = ["0x" + "22" * 20, "0x" + "33" * 20]

    class FakeRPC:
        def transaction(self, _tx_hash):
            return {"to": proxy, "blockNumber": "0x10"}

        def receipt(self, _tx_hash):
            return {"blockHash": "0x" + "44" * 32}

        def client_version(self):
            return "fake"

        def code(self, _address, _block):
            return "0x"

        def call(self, method, params):
            assert method == "debug_traceTransaction"
            options = params[1]
            if options.get("tracer") == "prestateTracer":
                return {
                    "pre": {proxy: {"storage": {"0x1": "0x0"}}},
                    "post": {proxy: {"storage": {"0x1": "0x7"}}},
                }
            logs = []
            for implementation in implementations:
                logs.extend(
                    [
                        {
                            "pc": 1,
                            "op": "DELEGATECALL",
                            "depth": 1,
                            "stack": ["0x0", implementation, "0xffff"],
                            "memory": [],
                        },
                        {
                            "pc": 2,
                            "op": "SSTORE",
                            "depth": 2,
                            "stack": ["0x7", "0x1"],
                            "memory": [],
                        },
                        {"pc": 3, "op": "STOP", "depth": 2, "stack": [], "memory": []},
                    ]
                )
            logs.append({"pc": 4, "op": "STOP", "depth": 1, "stack": [], "memory": []})
            return {"structLogs": logs}

    bundle = TraceProvider(FakeRPC()).transaction("0x" + "55" * 32)
    assert bundle.changes[0].write is None
    assert any("ambiguous final SSTORE attribution" in item for item in bundle.warnings)


def test_trace_marks_opcode_provenance_unavailable():
    address = "0x" + "11" * 20

    class FakeRPC:
        calls = 0

        def transaction(self, _tx_hash):
            return {"to": address, "blockNumber": "0x10"}

        def receipt(self, _tx_hash):
            return {"blockHash": "0x" + "44" * 32}

        def client_version(self):
            return "fake"

        def call(self, _method, _params):
            self.calls += 1
            if self.calls == 1:
                return {"pre": {}, "post": {}}
            raise RPCError("struct logger unavailable")

    bundle = TraceProvider(FakeRPC()).transaction("0x" + "55" * 32)
    assert bundle.provenance["opcodes"] == "unavailable"
