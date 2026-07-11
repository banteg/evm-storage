# evm-storage

`evm-storage` explains persistent EVM state using exact compiler layouts and
execution traces. Its primary command turns a transaction hash into typed,
human-readable state changes:

```console
$ evm-storage tx 0xe4e6…f04e3
0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48
  balanceAndBlacklistStates[0x8351…e0f7]  1810200        → 450982900
  balanceAndBlacklistStates[0x9322…2bd5]  7604022581466 → 7603573408766
```

Every result retains its raw address, slot, before/after words, executing code
address, storage address, compiler-layout provenance, and confidence. Unknown
slots remain visible as unresolved raw changes; the tool does not guess.

## Install

The project uses Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```console
uv tool install .
evm-storage --help
```

For development:

```console
uv sync --all-groups
uv run pytest
```

An archive node is required for old transactions. The default endpoint is
`http://127.0.0.1:8545`; override it with `EVM_STORAGE_RPC_URL` or `--rpc-url`.

## Decode a transaction

```console
evm-storage tx TX_HASH
evm-storage tx TX_HASH --json
```

By default, layouts are fetched without recompiling from Sourcify's v2
`fields=all` endpoint for the code addresses actually observed writing storage.
The compilation metadata preserves the compiler language and version alongside
the layout. Supply local artifacts when contracts are not verified or when
reproducibility matters:

```console
evm-storage tx TX_HASH \
  --layout 0xImplementation=out/Contract.sol/Contract.json
```

A verified Sourcify record can still have `storageLayout: null`, especially for
historical Vyper compilations. In that case the exact-version extractor remains
the fallback; the tool does not invent a layout from bytecode.

For transaction decoding, the Sourcify runtime-bytecode hash is compared with
`eth_getCode` at the transaction block. A mismatch rejects the layout; if code
validation is unavailable, decoded candidates are marked partial. JSON output
includes the language, compiler version, source, and validation metadata for
every address-bound layout.

The trace layer:

- gets exact pre/post slots with Geth's `prestateTracer` in diff mode;
- falls back to `trace_replayTransaction` state diffs;
- collects all `KECCAK256` preimages and `SSTORE` operations from a struct log;
- supports current Reth word-array memory and Geth 1.17.3+ byte-string memory;
- tracks call target, executing code, and storage context independently;
- attributes `DELEGATECALL` writes to implementation code and proxy storage;
- recognizes one-hop EIP-7702 code designators;
- falls back conservatively to ERC-1167, ERC-1967 implementation, and ERC-1967
  beacon inspection when opcode attribution is unavailable;
- rechecks the receipt block hash after tracing to detect a reorg.

No struct-logger output limit is set: recent Geth versions may silently stop
capturing when a limit is reached, which is unsuitable for exact decoding. The
HTTP response is instead read completely or rejected at the explicit 512 MiB
default ceiling (`--rpc-max-response-mib`).

## Normalize compiler layouts

Artifacts are converted into the stable `evm-storage/layout/v1` schema:

```console
evm-storage layout normalize artifact.json
evm-storage layout normalize artifact.json --contract src/Vault.sol:Vault -o layout.json
```

Accepted Solidity inputs include a raw `storageLayout`, Standard JSON output,
and Foundry-style artifacts. Vyper inputs include flat legacy layout output,
wrapped modern layouts, namespaced module layouts, and the isolated worker
format.

### Extract from source

Solidity 0.5.13+ exposes `storageLayout` through Standard JSON:

```console
evm-storage layout extract solidity Contract.sol \
  --solc ~/.solc/solc-0.8.29 \
  --contract Contract.sol:Contract \
  -o layout.json
```

Vyper is always run at the requested exact version in a separate uv
environment:

```console
evm-storage layout extract vyper Vault.vy --version 0.2.12 -o layout.json
evm-storage layout extract vyper Vault.vy --version 0.4.3  -o layout.json
evm-storage layout extract vyper contracts/Vault.vy --version 0.4.3 \
  --path . -o layout.json
```

The main process never imports a historical compiler. The worker selects a
compatible Python runtime and emits JSON over stdout. Currently tested epochs:

| Compiler | Extraction |
|---|---|
| Solidity `<0.5.13` | supply an external normalized layout |
| Solidity `0.5.13+` | native Standard JSON `storageLayout` |
| Vyper `0.1.0b17–0.2.12` | compiler `GlobalContext` introspection |
| Vyper `0.2.13–0.2.15` | annotated AST/data-position introspection |
| Vyper `0.2.16+` | native layout plus compiler type introspection |

The extractor accounts for historical Vyper behavior that generic Solidity
layout libraries typically miss:

- mappings hash `slot || key`, rather than Solidity's `key || slot`;
- through 0.2.12, structs, arrays, bytes, and strings add an extra
  `keccak256(parent)` storage root;
- the old `MappingType.__repr__` reversed key and value types;
- Vyper 0.2.16–0.3.1 duplicated the rendered `HashMap` suffix;
- Vyper's bytes/string reserved span changed at 0.3.0;
- Vyper primitives and fixed-array elements are not Solidity-packed.

Compiler-rendered type strings are diagnostic only. Legacy workers read the
structured key/value and member type objects before normalizing them.

For Vyper 0.2.16+, a path-aware compiler CLI fallback resolves project imports
when the single-source introspection worker cannot. The fallback preserves the
authoritative slot table; types that exist only in imported modules may remain
opaque unless the artifact already carries their structure.

## Read and snapshot known paths

Forward reads support scalars, packed fields, struct members, fixed/dynamic
arrays, nested mappings, both compiler hash orders, and historical Vyper
composite roots:

```console
evm-storage read 0xContract 'balances[0xAccount]' --layout layout.json
evm-storage read 0xContract 'records[7].owner' --layout layout.json --block 22000000
```

Snapshot an explicit list of paths:

```console
printf '%s\n' totalSupply 'balances[0xAccount]' > paths.txt
evm-storage snapshot 0xContract --layout layout.json --paths paths.txt > snapshot.json
```

`read` and `snapshot` resolve moving block tags to one numbered block, record
its hash and chain id, and recheck the hash after reading. Decimal `--block`
values are normalized to JSON-RPC hex quantities.

Compare two snapshots without an RPC connection:

```console
evm-storage diff before.json after.json
evm-storage diff before.json after.json --json
```

Mappings cannot be enumerated from Ethereum state alone. Snapshot input is
therefore explicit; transaction traces are the preferred source of newly
observed mapping keys.

## Resolution model

```text
compiler artifact ──→ normalized type graph ───────────┐
                                                       │
transaction ──→ exact state diff ──→ changed slots ────┼─→ typed paths
            └─→ opcode trace ──────→ hash preimages ───┤
            └─→ call contexts ─────→ code/storage owner┘
```

Solidity layouts retain byte offsets and recursive type-table references.
Vyper layouts are enriched from compiler type objects. The resolver handles:

- packed Solidity scalars and packed fixed arrays;
- structs and fixed arrays;
- Solidity hashed dynamic arrays;
- Vyper inline dynamic arrays and byte strings;
- mappings and nested mappings;
- mapping-to-struct paths;
- Solidity short/long bytes heads and traced long-data words;
- Vyper's historical hashed-composite dialect;
- proxy/diamond delegatecall storage contexts.

Solidity dynamic-array element paths are marked partial when the trace proves
the array root but does not expose an authoritative runtime length. Exact
mapping or fixed-layout candidates take precedence over those unbounded paths.

Long bytes/string data words are accepted only when the trace contains the
corresponding 32-byte base-slot preimage. This avoids false matches against
unrelated high-entropy mapping slots.

## Limits

- A layout describes compiler-managed storage. Arbitrary inline assembly and
  handwritten namespaced slots require an explicit layout or remain unresolved.
- Complete mapping enumeration is impossible without previously observed or
  user-supplied keys.
- Pre-0.5.13 Solidity has no native layout artifact and is not reconstructed
  from AST yet.
- Constructor address attribution for nested `CREATE`/`CREATE2` traces remains
  raw when the created address cannot be recovered safely.
- Reverted inner-frame preimages may be retained by opcode tracing. SSTORE code
  attribution is used only when an unambiguous observed write equals the
  authoritative final state value.
- Vyper 0.2.13 and 0.2.14 contain real storage-allocation bugs. Exact compiler
  extraction reports their actual positions rather than correcting them.

## Development and validation

```console
uv run ruff format --check src test
uv run ruff check src test
uv run pytest
uv build
```

The offline suite covers Keccak vectors, compiler-schema epochs, HashMap
serialization bugs, both mapping hash orders, historical Vyper composite roots,
packed fields, dynamic arrays, path reads, pre/post state diffs, both live
struct-log JSON shapes, and delegatecall attribution.

The original proof-of-concept fragments are preserved under `legacy/` and are
excluded from the distribution.

## Reference

[Data Representation in Solidity](https://ethdebug.github.io/solidity-data-representation/)
is the conceptual reference for packing, multivalue types, lookup roots, and
mapping-key padding. Exact compiler output remains authoritative for versioned
layout details and language-specific behavior.
