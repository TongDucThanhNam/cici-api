"""Developer-only progressive AST navigation commands."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ast-tree",
        description="Progressively explore this repository with ast-grep outline.",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("map", "imports", "show", "scan"),
        default="map",
        help="map a surface, inspect imports, show a file/symbol, or scan architecture rules",
    )
    parser.add_argument("paths", nargs="*", help="files/directories (defaults depend on mode)")
    parser.add_argument("--symbol", help="regex used to narrow `show` output")
    parser.add_argument(
        "--json",
        choices=("pretty", "stream", "compact"),
        help="emit ast-grep outline JSON",
    )
    return parser


def build_command(
    mode: str,
    paths: Sequence[str],
    *,
    executable: str = "ast-grep",
    symbol: str | None = None,
    json_style: str | None = None,
) -> list[str]:
    """Build the underlying command without invoking a shell."""

    selected_paths = list(paths)
    if mode == "scan":
        return [executable, "scan", *(selected_paths or ["."])]

    selected_paths = selected_paths or ["cici"]
    command = [executable, "outline", *selected_paths]
    if mode == "map":
        command.extend(["--items", "structure", "--view", "names"])
    elif mode == "imports":
        command.extend(["--items", "imports", "--view", "signatures"])
    else:
        command.extend(["--items", "all", "--view", "expanded" if symbol else "digest"])
        if symbol:
            command.extend(["--match", symbol])
    if json_style:
        command.append(f"--json={json_style}")
    return command


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    executable = shutil.which("ast-grep")
    if executable is None:
        print(
            "ast-tree requires ast-grep >= 0.45.1; install the dev extra with "
            "`python -m pip install -e .[dev]`.",
            file=sys.stderr,
        )
        return 2
    command = build_command(
        args.mode,
        args.paths,
        executable=executable,
        symbol=args.symbol,
        json_style=args.json,
    )
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
