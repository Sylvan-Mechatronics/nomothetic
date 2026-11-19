"""Filesystem roots for a nomon install.

``NOMON_HOME`` is the folder on the device that holds the nomon checkouts
(``nomothetic/``, ``autonomon/``, ``nomographic/``) and the ``media/`` tree.
It defaults to ``~/perceptua-nomon``; the deploy scripts in each repo read the
same variable from ``.env.device`` so the runtime and the deploy target agree.
"""

import os
from pathlib import Path

#: Fallback for ``NOMON_HOME`` when the variable is unset or empty.
DEFAULT_NOMON_HOME: str = "~/perceptua-nomon"


def nomon_home() -> Path:
    """Return the nomon root folder on this device.

    Reads ``NOMON_HOME`` (tilde-expanded), falling back to
    :data:`DEFAULT_NOMON_HOME` when unset or empty.

    Returns
    -------
    Path
        The expanded nomon root folder.
    """
    raw = os.environ.get("NOMON_HOME", "").strip() or DEFAULT_NOMON_HOME
    return Path(raw).expanduser()


def media_dir() -> Path:
    """Return the root folder for saved media (photos, videos, audio).

    ``NOMON_MEDIA_DIR`` (tilde-expanded) wins when set and non-empty;
    otherwise this is ``<NOMON_HOME>/media``.

    Returns
    -------
    Path
        The expanded media root folder.
    """
    raw = os.environ.get("NOMON_MEDIA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return nomon_home() / "media"
