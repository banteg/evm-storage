# Handoff: historical Vyper storage layouts in Sourcify

## Goal

Make Sourcify return accurate storage layouts for verified Vyper contracts compiled before
0.4.1. The contract page currently reports:

> Storage layout is only available for Vyper contracts compiled with version >=0.4.1.

This is a Sourcify extraction limitation, not a general Vyper limitation. Exact historical
compilers can recover useful layouts much further back. `evm-storage` has a tested reference
implementation covering Vyper 0.1.0b17 through current releases.

The preferred fix is backend-side extraction and persistence so every Sourcify API consumer
benefits. Do not reconstruct layout in the frontend.

## Repositories and current gates

Primary backend checkout:

- local: `/Users/banteg/dev/argotorg/sourcify`
- upstream: <https://github.com/ethereum/sourcify>
- current inspected revision: `ef273852f8971c7d7d24a62d88bf40f89847fa1d`

The backend gate is in:

- `packages/lib-sourcify/src/Compilation/VyperCompilation.ts`
- `VyperCompilation.initVyperJsonInput()` only adds `layout` to `outputSelection` when the
  semver-compatible compiler version is at least 0.4.1.

The resulting API artifact is assembled in:

- `packages/lib-sourcify/src/Verification/Verification.ts`
- it currently maps `VyperOutputContract.layout?.storage_layout` to
  `contractCompilerOutput.storageLayout`.

The public API type already permits `VyperStorageLayout`:

- `services/server/src/server/types.ts`
- `GET /server/v2/contract/{chainId}/{address}?fields=storageLayout`
- `fields=all` exposes the same field and is an important downstream consumer path.

The screenshot's frontend message is independently hard-coded in:

- <https://github.com/sourcifyeth/repo.sourcify.dev/blob/e07c58f4d7258321c7335fa0a6af2496df109d07/src/app/%5BchainId%5D/%5Baddress%5D/page.tsx>

Change the frontend only after the backend can distinguish "layout unavailable" from a
version-based assumption. Ideally render the returned layout whenever it exists and otherwise
say that no layout was recovered, without duplicating compiler-version policy.

## Proven extraction matrix

The implementation to study is in this repository:

- `src/evm_storage/compiler.py`: exact-version isolation and Python runtime selection
- `src/evm_storage/_vyper_worker.py`: compiler-internal extraction across historical epochs
- `src/evm_storage/layout/vyper.py`: normalization and historical serialization repairs
- `test/test_integration.py`: exact compiler matrix
- `test/test_vyper_layout.py`: schema and regression coverage
- `test/fixtures/vyper_legacy_composites.vy`: old composite-layout fixture

The tested epochs are:

| Vyper version | Extraction source |
|---|---|
| 0.1.0b17-0.2.12 | compiler `GlobalContext` / `_globals` positions |
| 0.2.13-0.2.15 | annotated AST and compiler-assigned data positions |
| 0.2.16-0.4.0 | native `layout` output, enriched from structured compiler types |
| 0.4.1+ | existing native standard-JSON layout path |

Representative versions already exercised by the integration suite are 0.1.0b17, 0.2.12,
0.2.15, 0.2.16, 0.3.2, and 0.4.3. The historical compilers are loaded as Python libraries in
isolated uv environments rather than imported into the main application process.

Sourcify does not have to adopt uv specifically. It does need an equivalent exact-version,
process-isolated adapter because historical Vyper Python APIs and supported Python runtimes
differ. First check whether Sourcify's existing compiler images can expose `vyper -f layout`
for 0.2.16-0.4.0; those versions should be the least invasive first slice. The older epochs
require compiler-library introspection.

## Correctness traps that must survive the port

Do not normalize historical Vyper layouts as if they were Solidity layouts.

- Vyper mappings hash `slot || key`; Solidity hashes `key || slot`.
- Through Vyper 0.2.12, composite declarations use an additional
  `keccak256(parent_slot)` root. A top-level composite still consumes one allocator root slot.
- Old `MappingType.__repr__` renders key and value in the wrong order. Read structured
  `keytype`/`valuetype` or `key_type`/`value_type` fields instead of trusting `str(type)`.
- Vyper 0.2.16-0.3.1 can emit a duplicated rendered `HashMap` suffix. Preserve structured type
  data or repair this known serialization defect.
- Bytes/string reserved spans changed at 0.3.0. In the legacy hashed dialect, `Bytes[65]`
  logically covers four words at its hashed root: one length word plus three data words.
- Vyper storage values and fixed-array elements are not Solidity-packed.
- Nonreentrant lock allocation and naming vary by compiler epoch and may be finalized lazily
  during code generation.
- Vyper 0.2.13 and 0.2.14 contain real compiler allocation bugs. Report the positions assigned
  by that exact compiler; do not silently "correct" them.
- Namespaced/module layouts in newer Vyper must retain their hierarchy or be flattened without
  relying on JSON insertion order. Slot order is authoritative.

The first Sourcify contribution may persist its existing `VyperStorageLayout` shape rather than
the richer `evm-storage/layout/v1` graph, but it must retain accurate slot, type, and span data.
If the existing schema cannot represent structured historical types safely, extend it rather
than falling back to lossy or incorrect rendered type strings.

## Suggested implementation plan

1. Add a version-aware Vyper layout extractor behind `VyperCompilation`, separate from bytecode
   verification. Keep the existing standard-JSON path for 0.4.1+.
2. For 0.2.16-0.4.0, invoke the exact compiler's native layout output and attach it to the
   compiled contract artifact even when standard JSON does not return it.
3. For 0.1.0b17-0.2.15, add an isolated helper that loads the exact Vyper package and emits only
   JSON. Port the epoch adapters from `_vyper_worker.py`; do not make the Node process import or
   share Python compiler state.
4. Normalize the helper result into Sourcify's `VyperStorageLayout`, then let the existing
   `Verification.export()` and database persistence path carry `storageLayout` into API v2.
5. Ensure extraction failure does not invalidate an otherwise valid contract verification.
   Record/log a precise extraction error and leave `storageLayout` null.
6. Add a backfill strategy for already verified contracts. New-verification support alone will
   not fix the large existing population shown by the UI. Decide between a resumable recompilation
   job and an explicit reprocessing endpoint; avoid per-page-request recompilation.
7. Update `repo.sourcify.dev` to render any returned layout and remove its hard-coded Vyper 0.4.1
   gate.

Keep layout derivation tied to the exact verified compiler version, sources, settings, compilation
target, and `storage_layout_overrides`. The layout artifact must come from the same compilation
identity as the bytecode match.

## Tests and acceptance criteria

Backend unit/integration coverage should include at least:

- output selection remains unchanged for versions that reject `layout` in standard JSON;
- 0.4.1+ continues through the current native path;
- exact extraction for 0.2.16, 0.2.15, and 0.2.12, plus one 0.1.x release;
- a nested `HashMap[address, HashMap[address, uint256]]` proving key/value order;
- mapping to struct, fixed array, `Bytes[65]`, and nonreentrant locks;
- the 0.2.16-0.3.1 duplicated-HashMap regression;
- one 0.2.13/0.2.14 fixture proving actual compiler-assigned positions are preserved;
- extraction failure leaves verification successful and `storageLayout` absent;
- API v2 returns the persisted historical Vyper layout through both
  `fields=storageLayout` and `fields=all`;
- a reprocessed historical contract returns a non-null layout without changing its verification
  match or bytecode artifacts;
- the frontend shows a returned pre-0.4.1 layout instead of the version warning.

Use the `evm-storage` compiler matrix as an oracle during the port. For shared fixtures, compare
the Sourcify result to `uv run evm-storage layout extract vyper SOURCE --version VERSION` after
normalizing field names. The final PR should document which earliest Vyper release is supported
and any compiler releases intentionally excluded.

## Scope boundaries

- This task is storage-layout extraction and delivery, not storage value decoding.
- Transient storage is separate and should retain its current compiler-version behavior.
- Do not infer layout from deployed bytecode.
- Do not make the frontend run a compiler.
- Do not let a supplemental layout failure downgrade a valid verification result.

## Useful context

- `evm-storage` uses Sourcify API v2 `fields=all` and currently receives
  `storageLayout: null` for many verified historical Vyper contracts.
- Data Representation in Solidity is useful for the generic storage model, but exact Vyper
  compiler behavior is authoritative:
  <https://ethdebug.github.io/solidity-data-representation/>
- Sourcify upstream: <https://github.com/ethereum/sourcify>
- Sourcify repository UI: <https://github.com/sourcifyeth/repo.sourcify.dev>
