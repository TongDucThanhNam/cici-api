# Code navigation and AST tooling

Use this guide when exploring an unfamiliar feature, locating ownership, or
planning a refactor. It is intentionally a broad-to-narrow menu: start at the
smallest level that can answer the question and disclose implementation only
when needed.

## One-time setup

Install the editable package and developer tooling:

```powershell
python -m pip install -e ".[dev]"
ast-tree --help
```

The `dev` extra installs `ast-grep` 0.45.x. `ast-tree` is a thin, cross-platform
wrapper around `ast-grep outline`; it reads the current checkout and does not
maintain a generated index that can become stale.

## Progressive-disclosure ladder

| Question | Smallest useful command |
| --- | --- |
| Where is a literal, filename, or symbol mentioned? | `rg -n '<text>'` or `rg --files` |
| What is in a package or directory? | `ast-tree map cici` |
| Which modules does a subtree import? | `ast-tree imports cici` |
| What declarations and members are in one file? | `ast-tree show cici/driver.py` |
| What is the shape of one known symbol? | `ast-tree show cici/driver.py --symbol CiciDriver` |
| Where does a syntax-shaped construct occur? | `ast-grep run -p '<pattern>' <paths>` |
| Where is a symbol referenced, and what type/call target is it? | LSP, compiler, or targeted source reads |

Examples:

```powershell
# Broad package map without implementation bodies
ast-tree map cici

# Dependency direction before moving a responsibility
ast-tree imports cici

# One class with direct members and source lines
ast-tree show cici/driver.py --symbol CiciDriver

# Machine-readable output for scripts/agents
ast-tree show cici/jobs.py --json stream

# Structural call search (ignores comments and strings)
ast-grep run -p '$OBJ.set($$$ARGS)' cici tests --json=stream
```

`outline` is syntax-local: it does not resolve references, inferred types,
dynamic imports, re-exports, or call graphs. After it identifies the likely
owner, use the compiler/LSP or read the narrow source range needed to confirm
behavior.

## Architecture checks

The root `sgconfig.yml` loads rules from `ast-grep/rules/`. Run them with:

```powershell
ast-tree scan
ast-grep test --skip-snapshot-tests
```

The initial rules protect the highest-value boundaries:

- Playwright stays behind `cici/driver.py`.
- FastAPI/Pydantic stay in `cici/server.py`.
- Click/Rich stay in `cici/cli.py`.
- browser operations are not parallelized with `asyncio.gather`.
- `concurrency.worker_count` remains `1` in both shipped configurations.

Add a rule only for a stable, syntax-verifiable invariant. Test new rules under
`ast-grep/rule-tests/`; use source tests or an LSP for constraints that require
types, scope, data flow, counts across files, or runtime behavior.

## Where to look next

After the AST map identifies a subsystem, follow the ownership and change-routing
tables in [architecture.md](architecture.md). Browser behavior has a separate
deeper guide in [ui-automation.md](ui-automation.md), and verification choices
are indexed in [testing.md](testing.md).
