"""Version-consistency guard — v0.3 PR 8.

`pyproject.toml` and `src/lightcrawl/__init__.py` drifted apart during v0.2/v0.3
development (0.2.0 vs 0.1.0). This test pins them together so a future bump can
never silently update one and forget the other. Fully offline.
"""

from __future__ import annotations

import sys
from pathlib import Path

import lightcrawl

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - project targets py311+
    import tomli as tomllib

EXPECTED = "0.3.0"
_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_package_version_is_current():
    assert lightcrawl.__version__ == EXPECTED


def test_pyproject_matches_package_version():
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    assert data["project"]["version"] == lightcrawl.__version__
