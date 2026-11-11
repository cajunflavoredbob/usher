"""User-facing copy calls the report a Ticket and carries no em dash."""
from __future__ import annotations

import ast
import pathlib

import webui
from settings import Settings

ROOT = pathlib.Path(__file__).resolve().parent.parent
EM_DASH = chr(0x2014)


def test_panel_toggle_reads_ticket_dms_and_keeps_its_settings_key():
    html = webui._settings_page(Settings())
    assert "Ticket DMs</label>" in html
    assert "Issue DMs" not in html
    assert 'name="tg_notify_issues"' in html


def _copy_strings(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], "value", None), ast.Constant):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            yield node


def test_no_em_dash_in_bot_or_panel_copy():
    offenders = []
    paths = sorted((ROOT / "bot").glob("*.py")) + [
        ROOT / "webui.py", ROOT / "http_util.py"]
    for path in paths:
        for node in _copy_strings(path):
            # The title separator list in ticket_flow splits typed queries; it is data.
            if EM_DASH in node.value and node.value != f" {EM_DASH} ":
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []

