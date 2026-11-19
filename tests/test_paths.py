"""Tests for the paths module (NOMON_HOME / NOMON_MEDIA_DIR resolution)."""

import os
from pathlib import Path
from unittest.mock import patch

from nomothetic.paths import DEFAULT_NOMON_HOME, media_dir, nomon_home


def _env_without(*names: str) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in names}


def test_nomon_home_default() -> None:
    """Unset NOMON_HOME falls back to the default, tilde-expanded."""
    with patch.dict(os.environ, _env_without("NOMON_HOME"), clear=True):
        assert nomon_home() == Path(DEFAULT_NOMON_HOME).expanduser()


def test_nomon_home_empty_uses_default() -> None:
    """An empty NOMON_HOME is treated as unset."""
    with patch.dict(os.environ, {"NOMON_HOME": "  "}):
        assert nomon_home() == Path(DEFAULT_NOMON_HOME).expanduser()


def test_nomon_home_override_expands_tilde() -> None:
    """NOMON_HOME is honoured and tilde-expanded."""
    with patch.dict(os.environ, {"NOMON_HOME": "~/sylvan-mechatronics"}):
        assert nomon_home() == Path.home() / "sylvan-mechatronics"


def test_media_dir_derives_from_nomon_home() -> None:
    """Without NOMON_MEDIA_DIR, media lives under NOMON_HOME."""
    env = _env_without("NOMON_MEDIA_DIR")
    env["NOMON_HOME"] = "/opt/nomon"
    with patch.dict(os.environ, env, clear=True):
        assert media_dir() == Path("/opt/nomon/media")


def test_media_dir_explicit_wins() -> None:
    """NOMON_MEDIA_DIR overrides the NOMON_HOME-derived default."""
    with patch.dict(os.environ, {"NOMON_HOME": "/opt/nomon", "NOMON_MEDIA_DIR": "~/media"}):
        assert media_dir() == Path.home() / "media"
