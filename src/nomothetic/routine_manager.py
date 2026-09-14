"""Supervisor for on-device autonomy-routine processes (autonomon plugins).

nomothetic launches and tracks the autonomon plugin as a child process — this is
**process supervision, not cognition** (autonomon ADR-004). All perception,
modelling, and planning stays in the launched ``autonomon`` process; this module
only spawns it, tracks it, and stops it.

Each routine maps to one ``nomon-autonomon`` subprocess. The plugin connects back
to this device's REST API (``NOMON_DEVICE_URL``) to read sensors and command
actuators, and reports its own lifecycle events to the routine log store. The
credentials and connection details are injected from this gateway's configuration
(:class:`RoutineManagerConfig`); the request payload only carries the routine name
and its parameters, so no secret ever travels over the start request.

**Heartbeat lease (the connection-loss guard).** A routine runs under a
*renewable* lease rather than a fixed cap: the operator (nomotactic) sends a
heartbeat (``POST /api/routines/heartbeat``) on an interval, and each one pushes
the deadline out by ``heartbeat_timeout_s``. While contact is maintained the
routine keeps running; if the heartbeats stop — the operator lost connection or
closed the app — the lease lapses and a per-process watchdog stops the routine.
An optional absolute ``max_duration_s`` bounds total runtime regardless of
heartbeats (off by default). This is the coarse, process-level safety net that
complements the fine-grained actuator lease watchdog in nomopractic (which halts
the motors within one lease TTL whenever the plugin stops issuing commands).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from nomothetic.routine_catalog import (
    catalog_path,
    published_autonomon_bin,
    published_params_schema,
)
from nomothetic.routine_log_store import (
    InvalidRoutineName,
    RoutineLogStore,
    validate_routine_name,
)

logger = logging.getLogger(__name__)

# Parent environment variables passed through to the plugin child. Deliberately
# minimal so the child does not inherit unrelated gateway secrets; the routine's
# own NOMON_* variables are layered on top in _build_env(). Autonomon-specific
# runtime config (e.g. the vision model/detector) is NOT listed here: nomothetic
# stays ignorant of the brain's internals (ADR-004/005). The autonomon CLI loads
# its own env file (/etc/autonomon/autonomon.env) for that.
_ENV_PASSTHROUGH = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "SSL_CERT_FILE")

# Floor for a client-supplied heartbeat timeout, so the connection-loss guard
# cannot be set uselessly low. The operator-configured default is exempt.
_MIN_HEARTBEAT_TIMEOUT_S = 5.0

# Review S-10: routine params are forwarded to the brain verbatim, so they are
# allow-listed here against the schema autonomon publishes. Keys must look like
# identifiers, values must be scalars, and — when a schema is published — every
# key must be declared with a matching type.
_PARAM_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_PARAM_STRING = 512
_SCHEMA_TYPES: dict[str, tuple[type, ...]] = {
    "number": (int, float),
    "integer": (int,),
    "string": (str,),
    "boolean": (bool,),
}


def validate_routine_params(params: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    """Reject routine params the published catalogue does not declare.

    Parameters
    ----------
    params : Mapping
        Client-supplied routine parameters.
    schema : Mapping
        ``params_schema`` from the published catalogue (``{}`` when none).

    Raises
    ------
    ValueError
        On a malformed key, a non-scalar or oversized value, an undeclared key
        (when a schema is published), or a type mismatch with the schema.
    """
    for key, value in params.items():
        if not isinstance(key, str) or not _PARAM_KEY_RE.match(key):
            raise ValueError(f"invalid routine param name {key!r}")
        if key == "routine":
            raise ValueError("'routine' is selected by the request, not a param")
        if isinstance(value, bool) or isinstance(value, (int, float, str)) or value is None:
            pass
        else:
            raise ValueError(f"routine param {key!r} must be a scalar (got {type(value).__name__})")
        if isinstance(value, str) and len(value) > _MAX_PARAM_STRING:
            raise ValueError(f"routine param {key!r} is too long (max {_MAX_PARAM_STRING} chars)")
        if not schema:
            continue
        spec = schema.get(key)
        if not isinstance(spec, Mapping):
            raise ValueError(f"unknown routine param {key!r}")
        expected = _SCHEMA_TYPES.get(str(spec.get("type", "")))
        if expected is None or value is None:
            continue
        if isinstance(value, bool) and bool not in expected:
            raise ValueError(f"routine param {key!r} must be {spec.get('type')}")
        if not isinstance(value, expected):
            raise ValueError(f"routine param {key!r} must be {spec.get('type')}")


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _resolve_autonomon_bin(catalog: Path | None) -> str:
    """Locate the ``nomon-autonomon`` console script, reading the catalogue live.

    autonomon and nomothetic are standalone projects with **separate venvs**
    (autonomon ADR-005), so the gateway cannot assume the CLI lives in its own venv.
    Resolution order:

    1. The absolute path autonomon advertises in its published catalogue
       (*catalog*) — the normal, fully-decoupled path.
    2. A script beside the running interpreter — legacy same-venv installs.
    3. The bare name, resolved via ``PATH`` at exec time.

    ``NOMON_AUTONOMON_BIN`` takes precedence over all of these; that override is
    applied by the caller, :meth:`RoutineManager._resolve_bin`.
    """
    published = published_autonomon_bin(catalog)
    if published:
        if Path(published).is_file():
            return published
        logger.warning(
            "published autonomon CLI %s is not an accessible file; "
            "falling back to a PATH lookup of 'nomon-autonomon'",
            published,
        )
    candidate = Path(sys.executable).resolve().parent / "nomon-autonomon"
    if candidate.is_file():
        return str(candidate)
    return "nomon-autonomon"


class _ProcessLike(Protocol):
    """The subset of :class:`asyncio.subprocess.Process` this module relies on.

    ``pid``/``returncode`` are declared as read-only properties so that
    :class:`asyncio.subprocess.Process` (whose ``returncode`` is a read-only
    property) satisfies the protocol.
    """

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    async def wait(self) -> int: ...

    def send_signal(self, sig: int) -> None: ...

    def kill(self) -> None: ...


# A launcher spawns the child and returns a process handle. Injectable so tests
# can run the full lifecycle without spawning real processes.
Launcher = Callable[[list[str], dict[str, str]], Awaitable[_ProcessLike]]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RoutineManagerError(Exception):
    """Base class for routine-manager failures."""


class RoutineAlreadyRunning(RoutineManagerError):
    """Raised when starting a routine that is already running."""

    def __init__(self, routine: str) -> None:
        super().__init__(f"routine {routine!r} is already running")
        self.routine = routine


class RoutineCredentialsError(RoutineManagerError):
    """Raised when no plugin credential is configured to authenticate the child."""


class RoutineLaunchError(RoutineManagerError):
    """Raised when the plugin subprocess could not be spawned."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutineManagerConfig:
    """Deployment configuration for launching autonomy plugins.

    Parameters
    ----------
    autonomon_bin : str or None
        Explicit override for the ``nomon-autonomon`` console-script path (from
        ``NOMON_AUTONOMON_BIN``). When ``None`` (the default), the path is resolved
        *live at each launch* from the catalogue autonomon publishes (its own venv's
        CLI path; autonomon ADR-005), falling back to a script beside the running
        interpreter, then the bare name. Resolving live — rather than freezing it at
        startup — means a catalogue published after this gateway started is still
        picked up, with no restart and regardless of deploy order.
    routine_catalog_path : pathlib.Path or None
        Path to autonomon's published routine catalogue, read live at launch to
        locate the CLI. Captured from ``NOMON_ROUTINE_CATALOG_PATH`` by
        :meth:`from_env`; ``None`` falls back to the shared default at read time.
    device_url : str
        Base URL the launched plugin uses to reach this device's REST API
        (passed as ``NOMON_DEVICE_URL``). Self-signed TLS is expected.
    device_id : str
        Device identifier stamped on the plugin's messages (``NOMON_DEVICE_ID``).
    plugin_key : str or None
        Path to the plugin's Ed25519 private key (``NOMON_PLUGIN_KEY``, preferred
        per ADR-019). Takes precedence over ``plugin_token`` when both are set.
    plugin_token : str or None
        Static device JWT (``NOMON_PLUGIN_TOKEN``), a fallback when no key is set.
    default_heartbeat_timeout_s : float
        Max seconds without a heartbeat before a routine is stopped, when the
        request does not specify one. The renewable lease window.
    heartbeat_timeout_ceiling_s : float
        Hard upper bound a per-request ``heartbeat_timeout_s`` is clamped to, so
        the connection-loss guard can never be widened arbitrarily.
    max_duration_s : float or None
        Optional absolute cap on total runtime regardless of heartbeats. ``None``
        (the default) means a routine runs as long as contact is maintained.
    grace_period_s : float
        Seconds to wait after SIGINT before escalating to SIGKILL on stop.
    """

    autonomon_bin: str | None = None
    routine_catalog_path: Path | None = None
    device_url: str = "https://127.0.0.1:8443"
    device_id: str = "nomon"
    plugin_key: str | None = None
    plugin_token: str | None = None
    default_heartbeat_timeout_s: float = 120.0
    heartbeat_timeout_ceiling_s: float = 3600.0
    max_duration_s: float | None = None
    grace_period_s: float = 5.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RoutineManagerConfig:
        """Build a config from environment variables, falling back to defaults."""
        env = os.environ if env is None else env

        def _float(name: str, default: float) -> float:
            raw = env.get(name)
            if raw is None:
                return default
            try:
                value = float(raw)
            except ValueError:
                logger.warning("%s=%r is not a number; using default %s", name, raw, default)
                return default
            if value <= 0:
                logger.warning("%s=%r must be > 0; using default %s", name, raw, default)
                return default
            return value

        def _optional_float(name: str) -> float | None:
            raw = env.get(name)
            if raw is None or not raw.strip():
                return None
            try:
                value = float(raw)
            except ValueError:
                logger.warning("%s=%r is not a number; ignoring (no absolute cap)", name, raw)
                return None
            if value <= 0:
                logger.warning("%s=%r must be > 0; ignoring (no absolute cap)", name, raw)
                return None
            return value

        return cls(
            autonomon_bin=env.get("NOMON_AUTONOMON_BIN") or None,
            routine_catalog_path=catalog_path(env),
            device_url=env.get("NOMON_AUTONOMON_DEVICE_URL", "https://127.0.0.1:8443"),
            device_id=env.get("NOMON_DEVICE_ID", "nomon"),
            plugin_key=env.get("NOMON_PLUGIN_KEY") or None,
            plugin_token=env.get("NOMON_PLUGIN_TOKEN") or None,
            default_heartbeat_timeout_s=_float("NOMON_ROUTINE_HEARTBEAT_TIMEOUT_S", 120.0),
            heartbeat_timeout_ceiling_s=_float("NOMON_ROUTINE_HEARTBEAT_CEILING_S", 3600.0),
            max_duration_s=_optional_float("NOMON_ROUTINE_MAX_DURATION_S"),
            grace_period_s=_float("NOMON_ROUTINE_STOP_GRACE_S", 5.0),
        )

    def has_credentials(self) -> bool:
        """Return whether a plugin credential (key or token) is configured."""
        return bool(self.plugin_key or self.plugin_token)


# ---------------------------------------------------------------------------
# Tracked process
# ---------------------------------------------------------------------------


@dataclass
class _RoutineProcess:
    """A running routine subprocess and its bookkeeping."""

    routine: str
    params: dict[str, Any]
    process: _ProcessLike
    started_at: str
    launch_id: str
    heartbeat_timeout_s: float
    max_duration_s: float | None
    started_monotonic: float
    last_heartbeat_monotonic: float
    stop_reason: str | None = None
    monitor_task: asyncio.Task[None] | None = None
    term_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def info(self) -> dict[str, Any]:
        """Return a JSON-serialisable status snapshot for this run."""
        return {
            "routine": self.routine,
            "launch_id": self.launch_id,
            "pid": self.process.pid,
            "started_at": self.started_at,
            "heartbeat_timeout_s": self.heartbeat_timeout_s,
            "max_duration_s": self.max_duration_s,
            "status": "running" if self.process.returncode is None else "stopped",
        }

    def seconds_remaining(self, now: float) -> float:
        """Seconds until the lease (and any absolute cap) expires, floored at 0."""
        remaining = (self.last_heartbeat_monotonic + self.heartbeat_timeout_s) - now
        if self.max_duration_s is not None:
            remaining = min(remaining, (self.started_monotonic + self.max_duration_s) - now)
        return max(0.0, remaining)


async def _default_launcher(argv: list[str], env: dict[str, str]) -> _ProcessLike:
    """Spawn the plugin, discarding stdout (events arrive via the push reporter).

    stderr is inherited so plugin tracebacks land in the gateway's journal; stdin
    is closed. stdout is discarded because lifecycle events are delivered to the
    routine log store over HTTP, not parsed from the pipe (and an unread pipe
    would eventually deadlock the child).
    """
    return await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=None,
    )


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class RoutineManager:
    """Launches, tracks, and stops autonomy-routine subprocesses.

    Parameters
    ----------
    config : RoutineManagerConfig
        Deployment configuration (binary, device URL, credentials, durations).
    log_store : RoutineLogStore, optional
        When provided, manager-initiated stops (operator request, max-duration,
        shutdown) are recorded so the *reason* a routine ended is visible even if
        the plugin could not report it (e.g. the operator lost connection).
    launcher : callable, optional
        ``(argv, env) -> awaitable[process]`` used to spawn the child. Defaults
        to a real subprocess launcher; injectable for tests.
    """

    def __init__(
        self,
        config: RoutineManagerConfig,
        log_store: RoutineLogStore | None = None,
        launcher: Launcher | None = None,
    ) -> None:
        self._config = config
        self._log_store = log_store
        self._launcher: Launcher = launcher or _default_launcher
        self._running: dict[str, _RoutineProcess] = {}
        self._lock = asyncio.Lock()

    # -- public API ---------------------------------------------------------

    async def start(
        self,
        routine: str,
        params: dict[str, Any] | None = None,
        heartbeat_timeout_s: float | None = None,
        max_duration_s: float | None = None,
    ) -> dict[str, Any]:
        """Launch *routine* as a plugin subprocess and begin its lease watchdog.

        Parameters
        ----------
        routine : str
            Routine name (validated; selects the autonomon routine factory).
        params : dict, optional
            Routine parameters forwarded verbatim to the plugin. The routine name
            is injected so the plugin selects the right factory.
        heartbeat_timeout_s : float, optional
            Max seconds without a heartbeat before the routine is stopped. The
            renewable lease window; defaults to the configured default and is
            clamped to the configured ceiling. The operator must call
            :meth:`heartbeat` within this window to keep the routine alive.
        max_duration_s : float, optional
            Absolute cap on total runtime regardless of heartbeats. Defaults to
            the configured value (``None`` = no cap; runs while contact holds).

        Returns
        -------
        dict
            Run info: ``routine``, ``launch_id``, ``pid``, ``started_at``,
            ``heartbeat_timeout_s``, ``max_duration_s``, ``status``.

        Raises
        ------
        InvalidRoutineName
            If *routine* is not a valid routine name.
        ValueError
            If *params* is not a dict, or a duration is non-positive.
        RoutineCredentialsError
            If no plugin credential is configured.
        RoutineAlreadyRunning
            If the routine is already running.
        RoutineLaunchError
            If the subprocess could not be spawned.
        """
        validate_routine_name(routine)
        params = params or {}
        if not isinstance(params, dict):
            raise ValueError("params must be a JSON object")
        schema = await asyncio.to_thread(published_params_schema, self._config.routine_catalog_path)
        validate_routine_params(params, schema)
        if not self._config.has_credentials():
            raise RoutineCredentialsError(
                "no plugin credential configured; set NOMON_PLUGIN_KEY or NOMON_PLUGIN_TOKEN"
            )
        heartbeat_timeout = self._resolve_heartbeat_timeout(heartbeat_timeout_s)
        max_duration = self._resolve_max_duration(max_duration_s)
        loop = asyncio.get_running_loop()

        # Resolve the CLI path *live*, at launch, from autonomon's published
        # catalogue (autonomon ADR-005). The catalogue can appear or change after this
        # gateway started — deploy order, or a reload that is not a full restart —
        # and the listing endpoint already reads it live, so the launch path must
        # too; resolving here avoids freezing a stale bare-name fallback at startup.
        # Off the event loop because it touches the filesystem.
        autonomon_bin = await asyncio.to_thread(self._resolve_bin)

        async with self._lock:
            existing = self._running.get(routine)
            if existing is not None and existing.process.returncode is None:
                raise RoutineAlreadyRunning(routine)

            argv = [autonomon_bin]
            env = self._build_env(routine, params)
            try:
                process = await self._launcher(argv, env)
            except (FileNotFoundError, PermissionError, OSError) as exc:
                raise RoutineLaunchError(f"could not launch {autonomon_bin!r}: {exc}") from exc

            now_mono = loop.time()
            entry = _RoutineProcess(
                routine=routine,
                params=params,
                process=process,
                started_at=_now_iso(),
                launch_id=uuid.uuid4().hex,
                heartbeat_timeout_s=heartbeat_timeout,
                max_duration_s=max_duration,
                started_monotonic=now_mono,
                last_heartbeat_monotonic=now_mono,
            )
            self._running[routine] = entry
            entry.monitor_task = asyncio.create_task(
                self._monitor(entry), name=f"routine-monitor:{routine}"
            )

        logger.info(
            "launched routine %r (pid=%s, heartbeat_timeout=%.0fs, max_duration=%s)",
            routine,
            process.pid,
            heartbeat_timeout,
            f"{max_duration:.0f}s" if max_duration is not None else "none",
        )
        return entry.info()

    async def heartbeat(self, routine: str) -> dict[str, Any] | None:
        """Renew a running routine's lease.

        Pushes the deadline out by ``heartbeat_timeout_s`` from now. Call this on
        an interval shorter than that window to keep the routine running.

        Parameters
        ----------
        routine : str
            Routine name (validated).

        Returns
        -------
        dict or None
            ``{routine, status, heartbeat_timeout_s, seconds_remaining}`` while
            running, or ``None`` if the routine is not currently running.
        """
        validate_routine_name(routine)
        loop = asyncio.get_running_loop()
        async with self._lock:
            entry = self._running.get(routine)
            if entry is None or entry.process.returncode is not None:
                return None
            now = loop.time()
            entry.last_heartbeat_monotonic = now
            return {
                "routine": entry.routine,
                "status": "running",
                "heartbeat_timeout_s": entry.heartbeat_timeout_s,
                "seconds_remaining": entry.seconds_remaining(now),
            }

    async def stop(self, routine: str) -> dict[str, Any] | None:
        """Stop one running routine. Returns its run info, or ``None`` if not running."""
        validate_routine_name(routine)
        async with self._lock:
            entry = self._running.get(routine)
            if entry is None or entry.process.returncode is not None:
                return None
            entry.stop_reason = entry.stop_reason or "operator_request"
        await self._terminate(entry)
        await self._await_monitor(entry)
        logger.info("stopped routine %r", routine)
        return entry.info()

    async def stop_all(self, reason: str = "operator_request") -> list[dict[str, Any]]:
        """Stop every running routine. Returns run info for each one stopped."""
        async with self._lock:
            entries = [e for e in self._running.values() if e.process.returncode is None]
            for entry in entries:
                entry.stop_reason = entry.stop_reason or reason
        for entry in entries:
            await self._terminate(entry)
            await self._await_monitor(entry)
        if entries:
            logger.info("stopped %d routine(s)", len(entries))
        return [entry.info() for entry in entries]

    async def list_running(self) -> list[dict[str, Any]]:
        """Return run info for every currently running routine."""
        async with self._lock:
            return [e.info() for e in self._running.values() if e.process.returncode is None]

    async def shutdown(self) -> None:
        """Stop all routines on gateway shutdown so none are orphaned."""
        await self.stop_all(reason="server_shutdown")

    # -- internals ----------------------------------------------------------

    def _resolve_heartbeat_timeout(self, requested: float | None) -> float:
        if requested is None:
            value = self._config.default_heartbeat_timeout_s
        else:
            value = float(requested)
            if value <= 0:
                raise ValueError("heartbeat_timeout_s must be greater than 0")
            # Floor a client-supplied value so the connection-loss guard stays
            # meaningful; the operator-configured default is trusted as-is.
            value = max(value, _MIN_HEARTBEAT_TIMEOUT_S)
        return min(value, self._config.heartbeat_timeout_ceiling_s)

    def _resolve_max_duration(self, requested: float | None) -> float | None:
        if requested is None:
            return self._config.max_duration_s
        value = float(requested)
        if value <= 0:
            raise ValueError("max_duration_s must be greater than 0")
        return value

    def _resolve_bin(self) -> str:
        """Resolve the ``nomon-autonomon`` path to exec for this launch.

        An explicit ``NOMON_AUTONOMON_BIN`` (captured on the config) always wins;
        otherwise the path is read live from autonomon's published catalogue, so a
        catalogue published after startup is picked up without a gateway restart.
        Touches the filesystem — call via :func:`asyncio.to_thread`.
        """
        if self._config.autonomon_bin:
            return self._config.autonomon_bin
        return _resolve_autonomon_bin(self._config.routine_catalog_path)

    def _build_env(self, routine: str, params: dict[str, Any]) -> dict[str, str]:
        """Build the child environment: a minimal passthrough plus routine vars."""
        env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
        env["NOMON_DEVICE_URL"] = self._config.device_url
        env["NOMON_DEVICE_ID"] = self._config.device_id
        env["NOMON_PLUGIN_PARAMS"] = json.dumps({**params, "routine": routine})
        # Prefer key-based auth; ensure only one credential reaches the child.
        if self._config.plugin_key:
            env["NOMON_PLUGIN_KEY"] = self._config.plugin_key
        elif self._config.plugin_token:
            env["NOMON_PLUGIN_TOKEN"] = self._config.plugin_token
        return env

    async def _monitor(self, entry: _RoutineProcess) -> None:
        """Await the child, enforcing the renewable heartbeat lease, then clean up.

        Sleeps until the current deadline (last heartbeat + timeout, capped by any
        absolute max). If the process is still alive when that elapses, the lease
        has either lapsed (stop it) or been extended by a heartbeat (loop and
        recompute). A natural exit or a manager-initiated stop ends the wait early.
        """
        loop = asyncio.get_running_loop()
        proc = entry.process
        try:
            while proc.returncode is None:
                deadline = entry.last_heartbeat_monotonic + entry.heartbeat_timeout_s
                if entry.max_duration_s is not None:
                    deadline = min(deadline, entry.started_monotonic + entry.max_duration_s)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=max(0.0, deadline - loop.time()))
                    break  # exited on its own, or via _terminate (stop/stop_all)
                except asyncio.TimeoutError:
                    reason = self._lapsed_reason(entry, loop.time())
                    if reason is None:
                        continue  # a heartbeat extended the lease; recompute
                    entry.stop_reason = entry.stop_reason or reason
                    logger.warning("routine %r %s; stopping", entry.routine, reason)
                    await self._terminate(entry)
                    break
        except asyncio.CancelledError:
            entry.stop_reason = entry.stop_reason or "server_shutdown"
            await self._terminate(entry)
            raise
        finally:
            async with self._lock:
                if self._running.get(entry.routine) is entry:
                    del self._running[entry.routine]
            self._record_stop(entry)

    @staticmethod
    def _lapsed_reason(entry: _RoutineProcess, now: float) -> str | None:
        """Return why the lease has lapsed at *now*, or ``None`` if still valid."""
        if (
            entry.max_duration_s is not None
            and now >= entry.started_monotonic + entry.max_duration_s
        ):
            return "max_duration_exceeded"
        if now >= entry.last_heartbeat_monotonic + entry.heartbeat_timeout_s:
            return "heartbeat_timeout"
        return None

    async def _terminate(self, entry: _RoutineProcess) -> None:
        """Stop the child gracefully (SIGINT), escalating to SIGKILL. Idempotent.

        SIGINT lets the autonomon CLI unwind its pipeline — which sends a final
        stop to the actuators — before exiting. If it does not exit within the
        grace period, it is force-killed; either way the nomopractic lease
        watchdog halts the motors once commands stop.
        """
        proc = entry.process
        async with entry.term_lock:
            if proc.returncode is not None:
                return
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(proc.wait(), timeout=self._config.grace_period_s)
            except asyncio.TimeoutError:
                logger.warning(
                    "routine %r did not exit within %.0fs; killing",
                    entry.routine,
                    self._config.grace_period_s,
                )
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

    @staticmethod
    async def _await_monitor(entry: _RoutineProcess) -> None:
        if entry.monitor_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await entry.monitor_task

    def _record_stop(self, entry: _RoutineProcess) -> None:
        """Record why a manager-initiated stop happened (best-effort).

        Only manager-initiated stops carry a ``stop_reason``; a routine that exits
        on its own already reports its own ``stopping``/``error`` event, so we do
        not duplicate it here. Recorded with no ``run_id`` so the plugin's run
        segmentation in the log store is left untouched.
        """
        if self._log_store is None or entry.stop_reason is None:
            return
        with contextlib.suppress(InvalidRoutineName):
            self._log_store.record(
                entry.routine,
                "stopping",
                data={
                    "reason": entry.stop_reason,
                    "returncode": entry.process.returncode,
                    "source": "nomothetic",
                },
                device_id=self._config.device_id,
            )
