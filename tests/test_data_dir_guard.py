"""A test process never defaults to the real data directory."""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _data_dir(code_prefix: str, env_extra: dict = None) -> str:
    import os

    env = {k: v for k, v in os.environ.items() if k != "MINGLY_DATA_DIR"}
    env.update(env_extra or {})
    out = subprocess.run(
        [sys.executable, "-c", f"{code_prefix}from backend import config; print(config.DATA_DIR)"],
        cwd=REPO, env=env, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip().splitlines()[-1]


def test_importing_backend_under_pytest_uses_a_temp_dir():
    # Simulates a scratch conftest / script run under pytest that skips tests/conftest.py
    d = _data_dir("import pytest; ")
    assert Path(d).name.startswith("mingly-test-data-")
    assert Path(d).resolve() != (REPO / "data").resolve()


def test_explicit_data_dir_wins(tmp_path):
    assert _data_dir("import pytest; ", {"MINGLY_DATA_DIR": str(tmp_path)}) == str(tmp_path)


def test_dev_run_still_uses_repo_data_dir():
    assert Path(_data_dir("")).resolve() == (REPO / "data").resolve()
