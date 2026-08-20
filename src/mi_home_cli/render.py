"""输出渲染：table / json / yaml / plain。"""
from __future__ import annotations

import json
import sys
from enum import Enum
from typing import Any, Sequence

from rich.console import Console
from rich.table import Table

console = Console()
err_console = Console(stderr=True)


class OutputFormat(str, Enum):
    table = "table"
    json = "json"
    yaml = "yaml"
    plain = "plain"


def _to_yaml(data: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(data, dict):
        if not data:
            return pad + "{}"
        lines = []
        for key, value in data.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{key}:")
                lines.append(_to_yaml(value, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_scalar(value)}")
        return "\n".join(lines)
    if isinstance(data, list):
        if not data:
            return pad + "[]"
        lines = []
        for item in data:
            if isinstance(item, (dict, list)):
                block = _to_yaml(item, indent + 1).lstrip()
                lines.append(f"{pad}- {block}")
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return "\n".join(lines)
    return pad + _scalar(data)


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def output(
    data: Any,
    fmt: OutputFormat,
    *,
    columns: Sequence[str] | None = None,
    title: str | None = None,
) -> None:
    """按格式输出。table 只对 list[dict] 和 dict 有意义。"""
    if fmt is OutputFormat.json:
        console.print_json(json.dumps(data, ensure_ascii=False, default=str))
        return
    if fmt is OutputFormat.yaml:
        print(_to_yaml(data))
        return
    if fmt is OutputFormat.plain:
        _print_plain(data)
        return
    _print_table(data, columns=columns, title=title)


def _print_plain(data: Any) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            print(f"{key}\t{_scalar(value)}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                print("\t".join(_scalar(v) for v in item.values()))
            else:
                print(_scalar(item))
    else:
        print(_scalar(data))


def _print_table(
    data: Any, *, columns: Sequence[str] | None = None, title: str | None = None
) -> None:
    if isinstance(data, dict):
        table = Table(show_header=False, title=title, box=None, pad_edge=False)
        table.add_column(style="cyan", no_wrap=True)
        table.add_column()
        for key, value in data.items():
            table.add_row(str(key), _scalar(value))
        console.print(table)
        return
    if isinstance(data, list):
        if not data:
            console.print("[dim]（空）[/dim]")
            return
        if not isinstance(data[0], dict):
            for item in data:
                console.print(_scalar(item))
            return
        cols = list(columns or data[0].keys())
        table = Table(title=title, header_style="bold")
        for col in cols:
            table.add_column(col)
        for row in data:
            table.add_row(*(_scalar(row.get(col)) for col in cols))
        console.print(table)
        return
    console.print(_scalar(data))


def raw(text: str) -> None:
    """原样输出一行，不折行、不高亮。

    授权 URL 很长，被 rich 折行后会插入真实换行符，用户复制粘贴就废了。
    """
    err_console.print(text, soft_wrap=True, highlight=False, markup=False)


def stream(message: str) -> None:
    """流式输出的一行，走 stdout（要能被管道接住）。"""
    console.print(message, highlight=False, soft_wrap=True)


def info(message: str) -> None:
    err_console.print(message)


def warn(message: str) -> None:
    err_console.print(f"[yellow]![/yellow] {message}")


def error(message: str) -> None:
    err_console.print(f"[red]✗[/red] {message}")


def success(message: str) -> None:
    err_console.print(f"[green]✓[/green] {message}")


def is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def mask(value: str | None, keep: int = 4) -> str:
    """打码 token 一类的敏感值。"""
    if not value:
        return "-"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * 8}{value[-keep:]}"
