"""Executes every ```python block in the README and docs, so the documented examples stay true."""
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FILES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_python_blocks_run(path, monkeypatch):
    monkeypatch.chdir(ROOT)  # snippets use repo-relative paths such as tests/fixtures
    blocks = re.findall(r"```python\n(.*?)```", path.read_text(), re.S)
    assert blocks, f"{path.name} has no python blocks"
    namespace: dict = {"__name__": "docs"}
    for i, code in enumerate(blocks):
        exec(compile(code, f"{path.name}#block{i}", "exec"), namespace)  # noqa: S102
    assert os.getcwd() == str(ROOT)
