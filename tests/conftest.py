import re
import shutil
import socket
from pathlib import Path

import pytest

from taskworker.config import Config
from taskworker.sandbox import server


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def sandbox_url() -> str:
    url = f"http://localhost:{_free_port()}"
    server.ensure_running(url)
    return url


@pytest.fixture
def config(sandbox_url: str, tmp_path: Path) -> Config:
    server.reset(sandbox_url)
    workspace = tmp_path / "workspace"
    shutil.copytree(Path(__file__).parent.parent / "workspace" / "shared", workspace / "shared")
    return Config(sandbox_url=sandbox_url, workspace=workspace, runs_dir=tmp_path / "runs",
                  lessons_path=tmp_path / "lessons.json", verify=False)


def ref(text: str, pattern: str) -> str:
    """Find the element ref on a snapshot line matching `pattern`."""
    for line in text.splitlines():
        if re.search(pattern, line):
            m = re.findall(r"\[(e\d+)\]", line)
            # On table rows, take the ref nearest the matched text.
            for cell in line.split("|"):
                if re.search(pattern, cell) and (mm := re.search(r"\[(e\d+)\]", cell)):
                    return mm.group(1)
            if m:
                return m[0]
    raise AssertionError(f"no element matching {pattern!r} in snapshot:\n{text[:3000]}")
