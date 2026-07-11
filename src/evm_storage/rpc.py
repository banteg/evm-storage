"""Small dependency-free Ethereum JSON-RPC client."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from evm_storage.errors import RPCError


@dataclass(slots=True)
class RPCClient:
    url: str
    timeout: float = 120.0
    max_response_bytes: int = 512 * 1024 * 1024
    headers: dict[str, str] = field(default_factory=dict)
    _request_id: int = 0

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        self._request_id += 1
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params or [],
            }
        ).encode()
        request = urllib.request.Request(
            self.url,
            data=payload,
            headers={"Content-Type": "application/json", **self.headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            try:
                error_raw = exc.read(self.max_response_bytes + 1)
            except (OSError, http.client.HTTPException) as read_exc:
                raise RPCError(
                    f"{method}: could not read HTTP {exc.code} response: {read_exc}"
                ) from read_exc
            if len(error_raw) > self.max_response_bytes:
                raise RPCError(
                    f"{method}: HTTP {exc.code} response exceeds explicit "
                    f"{self.max_response_bytes}-byte limit"
                ) from exc
            detail = error_raw.decode(errors="replace")
            raise RPCError(f"{method}: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise RPCError(f"{method}: could not reach {self.url}: {exc}") from exc
        if len(raw) > self.max_response_bytes:
            raise RPCError(
                f"{method}: response exceeds explicit {self.max_response_bytes}-byte limit"
            )
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RPCError(f"{method}: RPC returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise RPCError(f"{method}: RPC returned a non-object response")
        if "error" in result:
            error = result["error"]
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message", error)
                raise RPCError(f"{method}: RPC error {code}: {message}")
            raise RPCError(f"{method}: RPC error: {error}")
        if "result" not in result:
            raise RPCError(f"{method}: RPC response has no result")
        return result["result"]

    def client_version(self) -> str:
        return str(self.call("web3_clientVersion"))

    def chain_id(self) -> int:
        return int(self.call("eth_chainId"), 16)

    def transaction(self, tx_hash: str) -> dict[str, Any]:
        result = self.call("eth_getTransactionByHash", [tx_hash])
        if not isinstance(result, dict):
            raise RPCError(f"transaction not found: {tx_hash}")
        return result

    def receipt(self, tx_hash: str) -> dict[str, Any]:
        result = self.call("eth_getTransactionReceipt", [tx_hash])
        if not isinstance(result, dict):
            raise RPCError(f"receipt not found: {tx_hash}")
        return result

    def block(self, block: str = "latest") -> dict[str, Any]:
        result = self.call("eth_getBlockByNumber", [block, False])
        if not isinstance(result, dict):
            raise RPCError(f"block not found: {block}")
        return result

    def code(self, address: str, block: str = "latest") -> str:
        return str(self.call("eth_getCode", [address, block]))

    def storage(self, address: str, slot: int, block: str = "latest") -> int:
        return int(self.call("eth_getStorageAt", [address, hex(slot), block]), 16)
