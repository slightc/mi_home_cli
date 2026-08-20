import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mi_home_cli.store import Profile  # noqa: E402


@pytest.fixture()
def profile(tmp_path: Path) -> Profile:
    return Profile("default", root=tmp_path)
