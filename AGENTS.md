# Repository instructions

These instructions apply to the entire repository. Always communicate in English.

## Rules that apply to every change

- Preserve the single-consumer UI model. Cici exposes one chat surface, so do not
  increase `concurrency.worker_count` or introduce parallel browser operations.
- Keep selectors, model aliases, and timing values in `config.yaml`. Inspect the
  current DOM before changing UI automation behavior.
- Treat live generation as a side effect: it controls the user's signed-in Cici
  session, consumes account quota, and can take several minutes. Do not run
  `test_e2e.py`, `cici image`, or `cici video` unless the task explicitly needs it.
- Never commit credentials, session data, local file paths, generated media, logs,
  `.env` files, or `~/.cici/quota.json`.
- Preserve unrelated work in the tree and keep patches scoped to the request.

## Working method

1. Check `git status --short` before editing.
2. Use `rg` to locate candidates. Before reading a large or unfamiliar source
   file, run `ast-grep outline <path>`; narrow known symbols with
   `--match '<symbol>' --view expanded`. Use source reads, the compiler, or an LSP
   for behavior, types, references, and call graphs.
3. Read the relevant guide from the index below before changing that area.
4. Make the smallest coherent change and preserve API payloads, job states, CLI
   exit codes, and configuration compatibility unless the task changes the contract.
5. Run the smallest meaningful verification described in `docs/testing.md` and
   report both what ran and what could not run.

## Progressive-disclosure index

| Read this | When to read it |
| --- | --- |
| [Architecture and invariants](docs/architecture.md) | Changing request flow, job state, queueing, storage, driver boundaries, or file ownership |
| [Development guide](docs/development.md) | Setting up the project or changing Python, dependencies, configuration, API, or CLI behavior |
| [Testing and verification](docs/testing.md) | Before validating any change; especially before a live smoke test |
| [Cici UI automation](docs/ui-automation.md) | Changing CDP, Playwright flows, selectors, uploads, result detection, models, timing, or recovery |

Use `README.md` for the user-facing installation and usage contract. Keep it in
sync when commands, endpoints, supported models, or known limitations change.
