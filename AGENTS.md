# Harvester: first principles

Harvester exists to give proven media-harvesting machinery a durable home. Keep it small, understandable, resumable, and useful after long periods of neglect.

## 1. Prefer optional layering over strict dependency

A missing optional layer should remove a capability, not break Harvester.

- The CLI must work without any UI.
- The core must work without Pillow; image normalization may improve when Pillow is present.
- TMDB-only, TVDB-only, offline/status-only, and other partial configurations should remain useful.
- Future frontends should sit on top of the same callable jobs rather than becoming a prerequisite for them.

The desired failure mode is: "this capability is unavailable", not "the application is unavailable".

## 2. Standard library first -- and for the required path, standard library only

The required Harvester runtime should use Python's standard library only.

Optional enhancements may use an optional dependency only when:

- the import is lazy/local,
- absence is handled cleanly,
- the base behavior remains useful without it, and
- the optional package does not leak upward into the core API.

Pillow-style best-effort image treatment is the model here.

## 3. Preserve proven behavior before improving its shape

The scripts in `reference/` are behavioral evidence from real, large runs. They are not the target architecture, but their hard-won semantics matter.

During consolidation:

- preserve resumability, matching conservatism, caching, retry/backoff behavior, atomic writes, and skip-existing behavior unless there is a concrete reason to change them;
- do not rewrite a working algorithm merely to make it look more unified;
- add focused regression coverage around important behavior before or alongside structural refactors;
- if behavior intentionally changes, document why.

`reference/` is temporary and may be removed once equivalent behavior is established outside it.

## 4. Durable plain files are part of the design

Prefer boring, inspectable state: JSON work files/caches, NFO/XML, JPG/PNG, and ordinary directories.

- Write mutable state atomically.
- Treat interruption and restart as normal operation.
- Do not silently discard useful prior state after a transient provider failure.
- Existing filesystem artifacts are receipts; do not redownload/rewrite them without an explicit reason or overwrite request.
- Keep work/manifests understandable enough that a human can inspect them when Harvester itself is unavailable.

Avoid making an opaque database the sole source of truth unless a future requirement genuinely demands it.

## 5. Keep provider churn at the edges

TMDB, TVDB, and future remote services will change. Their authentication, request shapes, endpoint quirks, throttling rules, and response normalization should live behind narrow provider-facing code.

A future repair should ideally look like:

> "TMDB changed; repair the TMDB adapter."

not:

> "TMDB changed; rediscover the entire application."

Provider failure must not corrupt already-good local state.

## 6. The CLI is a frontend, not the engine

Harvester may soon deliver the same information to a UI, another local program, or both.

Core/job code should therefore expose callable operations and structured results/events. CLI-specific formatting belongs at the edge.

Prefer shapes like:

```python
result = job.run(config, reporter=reporter)
```

rather than burying the useful state exclusively in terminal output.

A console reporter can print progress today; a future UI reporter can turn the same events into progress bars, cards, or logs without changing the harvesting logic.

Do not introduce a web server, GUI toolkit, event framework, or other frontend dependency into the core merely in anticipation of a UI.

## 7. Keep the command surface obvious

There should be one obvious entry point and useful `--help` at every level.

Commands must not depend on the caller's current working directory. Resolve project/config/work paths deliberately.

Prefer a small vocabulary of explicit jobs over a pile of historical scripts or magic modes. Convenience commands may compose primitive jobs, but the meaningful phase boundaries should remain visible -- especially where scan/resolve and materialize/download are intentionally separate.

## 8. Credentials are optional capability inputs, never source code

Harvester should support a simple drop-in local credential file such as `keys_and_tokens.txt`, plus environment variables and explicit overrides where useful.

- Never commit credentials.
- Never require remote credentials for local/status/help functionality.
- Never print tokens, API keys, or authentication headers in ordinary logs.
- Keep credential files and runtime caches/work files ignored by Git.

## 9. Be polite to remote services and to time itself

The existing code already paid for lessons about large runs. Keep them.

- Cache expensive or repeatable API responses where appropriate.
- Resume instead of starting over.
- Back off on throttling/transient failures.
- Avoid needless repeated scans and downloads.
- Do not add concurrency merely because it is available; bounded, predictable behavior is preferable to hammering providers.

A run over thousands of records should remain practical and restartable.

## 10. Document decisions, not syntax

Use docstrings and comments generously when they preserve information a future maintainer cannot cheaply infer from the code.

Good subjects include:

- contracts and invariants,
- why a conservative match rule exists,
- resumability/failure semantics,
- provider quirks,
- non-obvious filesystem conventions,
- compatibility decisions,
- reasons an apparently simpler approach was rejected.

Do not narrate obvious mechanics such as "this line prints" or "loop over actors".

When behavior, CLI usage, configuration, file formats, or an important architectural decision changes, update the relevant help/docs in the same change. Do not knowingly leave stale instructions behind.

## 11. Prefer clarity over framework

Harvester is allowed to be straightforward Python.

Do not introduce abstraction solely to reduce line count or make unrelated provider workflows look identical. Extract common code when the common behavior is real and stable.

A six-years-later maintainer should be able to open the relevant module, understand the local problem, and repair it without first learning a private framework.

## 12. Make maintenance local

Keep configuration, provider logic, job orchestration, persistence helpers, and presentation concerns separated enough that a change in one does not require touching all the others.

Small, mechanically understandable interfaces are preferable to broad objects carrying hidden state.

The long-term test is simple: after a long internet sabbatical, Harvester should mostly need its edge adapters taught what the outside world looks like now. The local library, durable manifests, and core workflow should still make sense.
