"""Conservative static proxy recognition used when execution evidence is absent."""

from __future__ import annotations

import re
from dataclasses import dataclass

from eth_hash.auto import keccak

from evm_storage.errors import RPCError
from evm_storage.model import address_hex, normalize_address
from evm_storage.rpc import RPCClient

EIP1967_IMPLEMENTATION_SLOT = int.from_bytes(keccak(b"eip1967.proxy.implementation"), "big") - 1
EIP1967_BEACON_SLOT = int.from_bytes(keccak(b"eip1967.proxy.beacon"), "big") - 1
_MINIMAL_PROXY_RE = re.compile(
    r"363d3d373d3d3d363d73(?P<address>[0-9a-f]{40})5af43d82803e903d91602b57fd5bf3"
)


@dataclass(frozen=True, slots=True)
class ProxyResolution:
    kind: str
    implementation: str
    beacon: str | None = None


def resolve_proxy(rpc: RPCClient, address: str, *, block: str = "latest") -> ProxyResolution | None:
    address = normalize_address(address)
    try:
        code = rpc.code(address, block).removeprefix("0x").lower()
    except RPCError:
        return None
    if len(code) == 46 and code.startswith("ef0100"):
        return ProxyResolution("eip-7702", normalize_address("0x" + code[6:46]))
    match = _MINIMAL_PROXY_RE.match(code)
    if match:
        return ProxyResolution("erc-1167", normalize_address("0x" + match.group("address")))
    if "f4" not in code:
        return None
    try:
        implementation = rpc.storage(address, EIP1967_IMPLEMENTATION_SLOT, block)
    except RPCError:
        return None
    if implementation:
        try:
            return ProxyResolution("erc-1967", address_hex(implementation))
        except ValueError:
            return None
    try:
        beacon_value = rpc.storage(address, EIP1967_BEACON_SLOT, block)
    except RPCError:
        return None
    if not beacon_value:
        return None
    try:
        beacon = address_hex(beacon_value)
    except ValueError:
        return None
    try:
        response = rpc.call("eth_call", [{"to": beacon, "data": "0x5c60da1b"}, block])
        implementation = int(str(response), 16)
        if implementation:
            return ProxyResolution(
                "erc-1967-beacon",
                address_hex(implementation),
                beacon,
            )
    except (RPCError, TypeError, ValueError):
        return None
    return None
