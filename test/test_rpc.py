from __future__ import annotations

import http.client
import io
import urllib.error
from unittest.mock import patch

import pytest

from evm_storage.errors import RPCError
from evm_storage.rpc import RPCClient


def test_limits_http_error_response_body():
    error = urllib.error.HTTPError(
        "http://rpc.invalid",
        500,
        "error",
        {},
        io.BytesIO(b"12345"),
    )
    client = RPCClient("http://rpc.invalid", max_response_bytes=4)
    with (
        patch("urllib.request.urlopen", side_effect=error),
        pytest.raises(RPCError, match="HTTP 500 response exceeds explicit 4-byte limit"),
    ):
        client.call("debug_traceTransaction")


def test_wraps_truncated_http_error_response():
    class BrokenBody:
        def read(self, _limit):
            raise http.client.IncompleteRead(b"", 10)

        def close(self):
            pass

    error = urllib.error.HTTPError(
        "http://rpc.invalid",
        500,
        "error",
        {},
        BrokenBody(),
    )
    client = RPCClient("http://rpc.invalid", max_response_bytes=4)
    with (
        patch("urllib.request.urlopen", side_effect=error),
        pytest.raises(RPCError, match="could not read HTTP 500 response"),
    ):
        client.call("debug_traceTransaction")
