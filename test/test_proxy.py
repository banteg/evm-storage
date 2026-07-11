from evm_storage.proxy import EIP1967_IMPLEMENTATION_SLOT, resolve_proxy


class FakeRPC:
    def __init__(self, *, code="0x", storage=None, call_result="0x"):
        self._code = code
        self._storage = storage or {}
        self._call_result = call_result

    def code(self, _address, _block):
        return self._code

    def storage(self, _address, slot, _block):
        return self._storage.get(slot, 0)

    def call(self, _method, _params):
        return self._call_result


def test_resolves_eip7702_designator():
    implementation = "12" * 20
    result = resolve_proxy(FakeRPC(code="0xef0100" + implementation), "0x" + "34" * 20)
    assert result.kind == "eip-7702"
    assert result.implementation == "0x" + implementation


def test_resolves_erc1167_runtime():
    implementation = "56" * 20
    code = "0x363d3d373d3d3d363d73" + implementation + "5af43d82803e903d91602b57fd5bf3"
    result = resolve_proxy(FakeRPC(code=code), "0x" + "34" * 20)
    assert result.kind == "erc-1167"
    assert result.implementation == "0x" + implementation


def test_resolves_erc1967_slot():
    implementation = int("78" * 20, 16)
    result = resolve_proxy(
        FakeRPC(code="0xf4", storage={EIP1967_IMPLEMENTATION_SLOT: implementation}),
        "0x" + "34" * 20,
    )
    assert result.kind == "erc-1967"
    assert result.implementation == "0x" + "78" * 20


def test_rejects_extended_eip7702_designator():
    code = "0xef0100" + "12" * 20 + "00"
    assert resolve_proxy(FakeRPC(code=code), "0x" + "34" * 20) is None


def test_rejects_embedded_minimal_proxy_runtime():
    implementation = "56" * 20
    code = "0x00" + "363d3d373d3d3d363d73" + implementation + "5af43d82803e903d91602b57fd5bf3"
    assert resolve_proxy(FakeRPC(code=code), "0x" + "34" * 20) is None


def test_rejects_noncanonical_erc1967_word():
    implementation = (1 << 200) | int("78" * 20, 16)
    result = resolve_proxy(
        FakeRPC(code="0xf4", storage={EIP1967_IMPLEMENTATION_SLOT: implementation}),
        "0x" + "34" * 20,
    )
    assert result is None
