"""Public exception hierarchy."""


class EVMStorageError(Exception):
    """Base error for expected user-facing failures."""


class LayoutError(EVMStorageError):
    """A compiler layout is missing, ambiguous, or malformed."""


class CompilerError(EVMStorageError):
    """A compiler or isolated extraction worker failed."""


class RPCError(EVMStorageError):
    """An Ethereum JSON-RPC request failed."""


class TraceError(EVMStorageError):
    """A transaction trace could not be interpreted safely."""
