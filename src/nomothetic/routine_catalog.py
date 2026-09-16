"""Read the autonomy-routine catalogue that autonomon publishes to a shared file.

Decoupling (autonomon ADR-005): nomothetic (comms/service) and autonomon (autonomy) are
standalone projects that run from **separate virtualenvs**; nomothetic does *not*
import autonomon. Instead autonomon writes its catalogue — routine names, parameter
schemas, version, and the absolute path to its own ``nomon-autonomon`` CLI — to a
JSON file at a shared absolute path at deploy time, and this module reads it.

A missing, unreadable, or malformed file yields an empty catalogue (no routines)
rather than an error: a freshly provisioned device, or one with no autonomon
deployed yet, simply offers no routines until autonomon publishes one. See autonomon
ADR-004 (the brain owns the routines; the gateway only relays them) and autonomon ADR-005
(the file-based handoff).

The path is read from ``NOMON_ROUTINE_CATALOG_PATH`` (default
``/var/lib/nomon/routine_catalog.json``); the same default is set in both repos'
``.env.device`` files so the publishing side (autonomon) and the reading side
(nomothetic) always agree.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Shared default path. MUST match autonomon's ``routines.publish.DEFAULT_CATALOG_PATH``
# and the ``NOMON_ROUTINE_CATALOG_PATH`` default in both repos' ``.env.device`` files.
DEFAULT_CATALOG_PATH = "/var/lib/nomon/routine_catalog.json"

_EMPTY_CATALOG: dict[str, Any] = {"routines": [], "params_schema": {}, "version": None}


def catalog_path(env: Mapping[str, str] | None = None) -> Path:
    """Return the catalogue file path (``NOMON_ROUTINE_CATALOG_PATH`` or the default).

    Parameters
    ----------
    env : Mapping, optional
        Environment to read from; defaults to :data:`os.environ`. Threaded through
        so the routine manager can resolve the path from the same env mapping it is
        configured with.
    """
    env = os.environ if env is None else env
    return Path(env.get("NOMON_ROUTINE_CATALOG_PATH") or DEFAULT_CATALOG_PATH)


def _load_catalog_file(path: Path | None = None) -> dict[str, Any] | None:
    """Read and parse the published catalogue file, or ``None`` if unavailable.

    Returns ``None`` (logged, never raised) when the file is missing, unreadable,
    not valid JSON, or not a JSON object, so the endpoints degrade to an empty
    catalogue instead of failing.
    """
    path = catalog_path() if path is None else path
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.info("routine catalogue %s not found; no routines published yet", path)
        return None
    except OSError as exc:
        logger.warning("could not read routine catalogue %s: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("routine catalogue %s is not valid JSON: %s", path, exc)
        return None
    if not isinstance(data, Mapping):
        logger.warning("routine catalogue %s is not a JSON object; ignoring", path)
        return None
    return dict(data)


def autonomon_catalog() -> dict[str, Any]:
    """Return the published routine catalogue, projected to the API response shape.

    Returns
    -------
    dict
        ``{"routines": list[str], "params_schema": dict, "version": str | None}``.
        Empty (no routines) when no catalogue has been published. Internal fields
        of the file (e.g. ``autonomon_bin``) are deliberately not exposed here.
    """
    data = _load_catalog_file()
    if data is None:
        return dict(_EMPTY_CATALOG)
    return catalog_from_manifest(data)


def catalog_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Project a catalogue mapping into the API response shape.

    Defensive about field presence/types so a catalogue from a newer or older
    autonomon never breaks the endpoint.
    """
    routines = manifest.get("routines", [])
    params_schema = manifest.get("params_schema", {})
    return {
        "routines": [str(r) for r in routines] if isinstance(routines, (list, tuple)) else [],
        "params_schema": dict(params_schema) if isinstance(params_schema, Mapping) else {},
        "version": manifest.get("version"),
    }


def published_params_schema(path: Path | None = None) -> dict[str, Any]:
    """Return the published ``params_schema`` mapping, or ``{}`` when unavailable.

    Used to allow-list routine params before they reach the brain (review S-10).

    Parameters
    ----------
    path : pathlib.Path, optional
        Catalogue file to read; defaults to :func:`catalog_path`.
    """
    data = _load_catalog_file(path)
    if data is None:
        return {}
    schema = data.get("params_schema", {})
    return dict(schema) if isinstance(schema, Mapping) else {}


def published_autonomon_bin(path: Path | None = None) -> str | None:
    """Return the ``nomon-autonomon`` path the published catalogue advertises.

    nomothetic launches a routine by exec'ing autonomon's own venv CLI (the two
    venvs are separate), and autonomon publishes that absolute path in the
    catalogue so the gateway needs no autonomon install of its own. Returns
    ``None`` when no catalogue is published or it carries no usable path.

    Parameters
    ----------
    path : pathlib.Path, optional
        Catalogue file to read; defaults to :func:`catalog_path`.
    """
    data = _load_catalog_file(path)
    if data is None:
        return None
    bin_path = data.get("autonomon_bin")
    return bin_path if isinstance(bin_path, str) and bin_path else None
