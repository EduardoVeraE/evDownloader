# Shared download boundaries

`service.py` is the compatibility facade and composition root. Provider selection,
DRM preflight, browser lifetime, structure caching, and final console/exception
delivery remain there. Existing service imports and injected test seams remain valid.

## Review path

| Boundary | Contract and responsibility |
|---|---|
| `orchestration.py` | Typed course traversal and unit coordination via explicit async operations; no provider registry, HTTP, or publication implementation. |
| `results.py` | `VideoOutcome`, `UnitOutcome`, `DownloadOutcome`, subtitle reports, failure inventory, and strict aggregate completion policy. No I/O. |
| `url_policy.py` | Existing authority validation and credential-header rules; browser cookie scoping remains shared. No duplicate provider policies. |
| `resource_download.py` | Existing attachment transfer, redirect/credential checks, bounded streaming, and atomic binary publication. Reused unchanged. |
| `subtitle_transfer.py` | Typed subtitle HTTP observations, redirects, response closure, and shared URL/credential policy. No filesystem writes. |
| `artifacts.py` | Title-safe artifact paths, atomic UTF-8 subtitle/manifest publication, and existing direct-write extras. No network or completion policy. |

`service.py` composes video resolution, subtitle validation/recovery, extras, and
these boundaries. The injected operations are fixed typed callables, not plugins.

## Preserved behavior

- Required video failures and existing extras/resource/subtitle failures still make
  a download incomplete. Optional failed page capture remains inventory-only.
- A reached video limit still stops traversal immediately, including later extras.
- Udemy's confirmed subtitle inventory, existing-media recovery, DRM preflight,
  sanitized failed-artifact inventory, and Platzi capture behavior remain intact.
- Atomic publication failures retain the old target and remove staging files.
  Optional extras retain direct-write behavior; this refactor does not promise
  transactionality across all course artifacts.

## Reconciliation and limits

The `evd-lp2` sibling worktree was inspected, not bulk-merged. Its result contracts
are **not** integrated: they make subtitle/resource failures nonfatal, continue
traversal after limits, and change setup and manifest failure handling. Those are
behavior changes, not prerequisites for extracting boundaries. This checkout's
newer strict failure and inventory semantics take precedence. The existing typed
outcomes were extracted instead; tuple-compatible named video/HTTP results add
clarity without changing callers. A future warning-policy change needs explicit
approval and separate CLI/exit regression coverage.

Failure reason strings remain compatible with existing reports rather than adopting
the sibling's incompatible reason enums. Subtitle HTTP bodies are checked after
buffering (the declared length is checked first); attachment transfer is streamed.
No live authenticated provider access is needed for the offline regression suite.
