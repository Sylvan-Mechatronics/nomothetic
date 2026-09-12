"""AI chat-command relay: Claude turns operator chat into validated device commands.

This module powers ``POST /api/ai/command`` (see :mod:`nomothetic.ai_routes`).
It runs a small agentic loop against the Anthropic Messages API: the operator's
chat turns go in, Claude may call a fixed set of tools, and every tool maps
onto the *same* validated device operations the app's buttons use — ``drive``,
``steer``, and camera moves under TTL leases, sensor reads, and autonomy-routine
start/stop.  The loop holds no robot state and interprets no sensor data;
cognition for autonomy stays in autonomon (ADR-004).  This is an operator
convenience relay, nothing more.

Security posture
----------------
* The tool surface is destructive-free by construction: no MCU reset, no
  calibration writes, no raw servo pulses.  Movement goes through the same
  range validation and TTL watchdog as the manual controls, so a runaway
  conversation cannot outlive its lease.
* The Anthropic API key is user-supplied.  :class:`AiKeyStore` persists it in
  a ``0600`` file (default ``/var/lib/nomon/ai_api_key``), never logs it, and
  never returns it through the API — only presence/source metadata.
* ``ANTHROPIC_API_KEY`` in the service environment acts as an operator-level
  fallback when no key has been stored.

The ``anthropic`` SDK is an optional dependency (``nomothetic[ai]``).  When it
is missing this module still imports and key management still works, but
running a command raises :class:`AiUnavailableError` (mapped to HTTP 503).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import stat
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

try:
    import anthropic
except ImportError:  # pragma: no cover - exercised by patching anthropic to None
    anthropic = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_DEFAULT_KEY_PATH = "/var/lib/nomon/ai_api_key"
_MIN_STORED_KEY_LENGTH = 20
_MAX_STORED_KEY_LENGTH = 200

_DEFAULT_MODEL = "claude-sonnet-5"
_DEFAULT_MAX_TOKENS = 2048
_DEFAULT_MAX_TOOL_ITERATIONS = 8
_REQUEST_TIMEOUT_S = 120.0

_SYSTEM_PROMPT = """\
You are the chat assistant on board a nomon robot — a small Raspberry Pi rover with drive
motors, steering, a pan/tilt camera head, three downward grayscale sensors, and a forward
ultrasonic distance sensor. You act on the robot only through the provided tools.

Ground rules:
- Movement tools take a ttl_ms lease (100-5000 ms). The robot stops automatically when the
  lease expires, so motion continues only as long as you keep issuing commands. Prefer short
  leases, and call stop immediately if the user says stop or anything seems wrong.
- Angles are degrees 0-180 with 90 = centre (steering straight ahead, camera level).
- Before driving forward more than a nudge, check read_ultrasonic and do not drive toward an
  obstacle closer than about 20 cm.
- Grayscale readings are raw ADC values; their meaning (line/cliff) is robot-specific, so
  report the numbers rather than guessing what they imply.
- You cannot calibrate the robot, reset its firmware, or change system settings from chat;
  say so plainly if asked.
- Routines are autonomous behaviours run by the robot's autonomy stack. A routine you start
  is auto-stopped if the operator's app stops sending heartbeats for it.
- Keep replies to one to three short sentences. Report what you actually did, including any
  tool failures, and include sensor values when you read them.
"""


# ============================================================================
# Errors
# ============================================================================


class AiUnavailableError(Exception):
    """AI support is not available on this device (anthropic SDK not installed)."""


class AiKeyStoreError(Exception):
    """The API key file could not be written or removed."""


class AiProviderError(Exception):
    """The Anthropic API request failed (network, rate limit, or server error)."""


class AiProviderAuthError(AiProviderError):
    """The Anthropic API rejected the configured API key."""


class ToolExecutionError(Exception):
    """A tool could not run; the message is reported back to the model."""


# ============================================================================
# API key store
# ============================================================================


def validate_api_key_format(api_key: str) -> str:
    """Validate the shape of a user-supplied Anthropic API key.

    Only the format is checked (no network call), so a typo inside an
    otherwise well-formed key is caught later by the provider (HTTP 502).

    Parameters
    ----------
    api_key : str
        Raw key as supplied by the user; surrounding whitespace is stripped.

    Returns
    -------
    str
        The stripped key.

    Raises
    ------
    ValueError
        If the key does not look like an Anthropic API key.
    """
    key = api_key.strip()
    if not key.startswith("sk-ant-"):
        raise ValueError("API key must start with 'sk-ant-'")
    if not _MIN_STORED_KEY_LENGTH <= len(key) <= _MAX_STORED_KEY_LENGTH:
        raise ValueError(
            f"API key length must be between {_MIN_STORED_KEY_LENGTH} and "
            f"{_MAX_STORED_KEY_LENGTH} characters"
        )
    if any(ch.isspace() or not ch.isprintable() for ch in key):
        raise ValueError("API key must be a single line of printable characters")
    return key


class AiKeyStore:
    """Persistent store for the user-supplied Anthropic API key.

    The key is stored in a ``0600`` file at *path* (default:
    ``/var/lib/nomon/ai_api_key``, override with ``NOMON_AI_API_KEY_PATH``)
    using the same atomic ``mkstemp`` + ``rename`` pattern as
    :class:`nomothetic.device_jwt.DeviceJwtSecretStore`.  Unlike the JWT
    secret store, write failures raise :class:`AiKeyStoreError` — a user who
    submits a key must learn immediately if it could not be persisted.

    The key value itself is never logged and never leaves this module except
    as the ``api_key`` argument to the Anthropic client.

    Parameters
    ----------
    path : str, optional
        Absolute path of the key file.  Defaults to
        ``$NOMON_AI_API_KEY_PATH`` or ``/var/lib/nomon/ai_api_key``.
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path or os.environ.get("NOMON_AI_API_KEY_PATH", _DEFAULT_KEY_PATH)

    def load(self) -> str | None:
        """Return the stored key, or ``None`` if absent or implausibly short."""
        try:
            with open(self._path, encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError:
            return None
        if len(value) < _MIN_STORED_KEY_LENGTH:
            logger.warning(
                "Stored AI API key at %s is too short (%d chars); treating as unset",
                self._path,
                len(value),
            )
            return None
        return value

    def resolve(self) -> tuple[str, str] | None:
        """Resolve the effective API key and where it came from.

        A user-stored key takes precedence over the ``ANTHROPIC_API_KEY``
        environment variable (the operator-level fallback).

        Returns
        -------
        tuple of (str, str) or None
            ``(key, source)`` with source ``"stored"`` or ``"env"``, or
            ``None`` when no key is configured at all.
        """
        stored = self.load()
        if stored is not None:
            return stored, "stored"
        env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if env_key:
            return env_key, "env"
        return None

    def source(self) -> str | None:
        """Return ``"stored"``, ``"env"``, or ``None`` without exposing the key."""
        resolved = self.resolve()
        return resolved[1] if resolved is not None else None

    def save(self, api_key: str) -> None:
        """Atomically persist *api_key* with ``0600`` permissions.

        Parameters
        ----------
        api_key : str
            Key to store; callers should validate it with
            :func:`validate_api_key_format` first.

        Raises
        ------
        AiKeyStoreError
            If the key directory is missing or the write fails.
        """
        target_dir = os.path.dirname(self._path)
        if not os.path.isdir(target_dir):
            raise AiKeyStoreError(f"key directory {target_dir} does not exist on this device")

        fd: int | None = None
        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".ai_api_key_")
            os.write(fd, api_key.encode("utf-8"))
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
            os.close(fd)
            fd = None
            os.rename(tmp_path, self._path)
            tmp_path = None
            logger.info("AI API key stored at %s", self._path)
        except OSError as e:
            raise AiKeyStoreError("failed to persist the AI API key") from e
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def clear(self) -> bool:
        """Remove the stored key file.

        Returns
        -------
        bool
            ``True`` if a key file was removed, ``False`` if none existed.

        Raises
        ------
        AiKeyStoreError
            If the file exists but could not be removed.
        """
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            return False
        except OSError as e:
            raise AiKeyStoreError("failed to remove the stored AI API key") from e
        logger.info("AI API key removed from %s", self._path)
        return True


# ============================================================================
# Tool argument models (bounds mirror the manual-control request models in api.py)
# ============================================================================


class _EmptyArgs(BaseModel):
    """No arguments."""

    model_config = ConfigDict(extra="forbid")


class _DriveArgs(BaseModel):
    """Arguments for ``drive`` (mirrors ``DriveRequest``)."""

    model_config = ConfigDict(extra="forbid")

    speed_pct: float = Field(
        ...,
        ge=-100.0,
        le=100.0,
        description="Signed speed: -100 = full reverse, +100 = full forward.",
    )
    ttl_ms: int = Field(
        default=500,
        ge=100,
        le=5000,
        description="Motion lease in ms; the robot stops automatically when it expires.",
    )


class _AngleArgs(BaseModel):
    """Arguments for ``steer`` / ``pan_camera`` / ``tilt_camera`` (mirror api.py bounds)."""

    model_config = ConfigDict(extra="forbid")

    angle_deg: float = Field(
        ...,
        ge=0.0,
        le=180.0,
        description="Angle in degrees 0-180 (90 = centre).",
    )
    ttl_ms: int = Field(
        default=500,
        ge=100,
        le=5000,
        description="Servo lease in ms.",
    )


class _StartRoutineArgs(BaseModel):
    """Arguments for ``start_routine`` (mirrors ``RoutineStartRequest``)."""

    model_config = ConfigDict(extra="forbid")

    routine: str = Field(
        ..., min_length=1, max_length=64, description="Routine name from list_routines."
    )
    params: dict[str, Any] = Field(
        default_factory=dict, description="Routine parameters (see the routine's param schema)."
    )


class _StopRoutineArgs(BaseModel):
    """Arguments for ``stop_routine``."""

    model_config = ConfigDict(extra="forbid")

    routine: str = Field(..., min_length=1, max_length=64, description="Routine name to stop.")


# ============================================================================
# Helpers
# ============================================================================


def _jsonable(value: Any) -> Any:
    """Convert HAT result objects (dataclasses, containers) to JSON-safe values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    """Read an integer env var, falling back to *default* outside [lo, hi] or on junk."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Env var %s=%r is not an integer; using default %d", name, raw, default)
        return default
    if not lo <= value <= hi:
        logger.warning(
            "Env var %s=%r outside allowed range %d-%d; using default %d",
            name,
            raw,
            lo,
            hi,
            default,
        )
        return default
    return value


def _default_client_factory(api_key: str) -> Any:
    """Build an ``AsyncAnthropic`` client for one command run."""
    if anthropic is None:  # pragma: no cover - callers check availability first
        raise AiUnavailableError("anthropic SDK is not installed")
    return anthropic.AsyncAnthropic(api_key=api_key, timeout=_REQUEST_TIMEOUT_S, max_retries=1)


def _map_provider_error(exc: Exception) -> AiProviderError | None:
    """Map an anthropic SDK exception to our error types (``None`` = not ours)."""
    if anthropic is None:
        return None
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return AiProviderAuthError("the AI provider rejected the configured API key")
    if isinstance(exc, anthropic.APIError):
        return AiProviderError(f"AI provider request failed: {exc}")
    return None


# ============================================================================
# Service
# ============================================================================

# A tool runner takes its validated argument model and returns a raw result.
# Typed with Any because each runner accepts its own concrete args model.
_ToolRunner = Callable[[Any], Awaitable[Any]]


class AiCommandService:
    """Claude agentic loop over a destructive-free robot tool registry.

    Parameters
    ----------
    hat_call : callable
        Async callable ``(method, *args, **kwargs)`` that invokes a HatClient
        method off the event loop — normally ``api._hat_call``, which already
        maps HAT errors to ``HTTPException`` (503/500).
    get_routine_manager : callable, optional
        Zero-arg callable returning the app's ``RoutineManager`` (or ``None``
        when routine control is unavailable).
    get_catalog : callable, optional
        Zero-arg blocking callable returning autonomon's routine catalogue
        (``nomothetic.routine_catalog.autonomon_catalog``).
    model : str, optional
        Anthropic model ID.  Defaults to ``$NOMON_AI_MODEL`` or
        ``claude-sonnet-5``.
    max_tokens : int, optional
        Per-response token cap.  Defaults to ``$NOMON_AI_MAX_TOKENS`` or 2048.
    max_tool_iterations : int, optional
        Cap on model↔tool round trips per command.  Defaults to
        ``$NOMON_AI_MAX_TOOL_ITERATIONS`` or 8.
    client_factory : callable, optional
        ``api_key -> client`` factory, injectable for tests.  The client must
        expose ``messages.create(**kwargs)`` and an awaitable ``close()``.
    """

    def __init__(
        self,
        hat_call: Callable[..., Awaitable[Any]],
        get_routine_manager: Callable[[], Any] | None = None,
        get_catalog: Callable[[], dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        max_tool_iterations: int | None = None,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._hat_call = hat_call
        self._get_routine_manager = get_routine_manager
        self._get_catalog = get_catalog
        self._model = model or os.environ.get("NOMON_AI_MODEL", _DEFAULT_MODEL)
        self._max_tokens = max_tokens or _int_env(
            "NOMON_AI_MAX_TOKENS", _DEFAULT_MAX_TOKENS, 256, 8192
        )
        self._max_tool_iterations = max_tool_iterations or _int_env(
            "NOMON_AI_MAX_TOOL_ITERATIONS", _DEFAULT_MAX_TOOL_ITERATIONS, 1, 25
        )
        self._client_factory = client_factory
        self._tools: dict[str, tuple[type[BaseModel], _ToolRunner]] = {}
        self._tool_params: list[dict[str, Any]] = []
        self._register_tools()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def tool_names(self) -> list[str]:
        """Names of the registered tools (stable order)."""
        return [param["name"] for param in self._tool_params]

    async def run_command(self, messages: list[dict[str, Any]], api_key: str) -> dict[str, Any]:
        """Run one chat command through the Claude tool loop.

        Parameters
        ----------
        messages : list of dict
            Prior conversation as ``{"role": "user"|"assistant", "content": str}``
            turns, ending with the new user message.  Tool-use blocks never
            cross requests; each call is one self-contained agentic run.
        api_key : str
            Anthropic API key to use for this run.

        Returns
        -------
        dict
            ``reply`` (final assistant text), ``actions`` (ordered tool-call
            records), ``model``, and ``stop_reason``.

        Raises
        ------
        AiUnavailableError
            If the anthropic SDK is not installed.
        AiProviderAuthError
            If the provider rejects *api_key*.
        AiProviderError
            On other provider failures (network, rate limit, server error).
        """
        factory = self._client_factory
        if factory is None:
            if anthropic is None:
                raise AiUnavailableError(
                    "AI support is not installed on this device (install nomothetic[ai])"
                )
            factory = _default_client_factory
        client = factory(api_key)
        convo: list[dict[str, Any]] = [dict(message) for message in messages]
        actions: list[dict[str, Any]] = []
        try:
            for _ in range(self._max_tool_iterations):
                response = await self._create_message(client, convo)
                if getattr(response, "stop_reason", None) != "tool_use":
                    return self._final_result(response, actions)
                convo.append({"role": "assistant", "content": response.content})
                tool_results = await self._run_tool_blocks(response.content, actions)
                convo.append({"role": "user", "content": tool_results})
            logger.warning(
                "AI command hit the tool-iteration limit (%d)", self._max_tool_iterations
            )
            return {
                "reply": (
                    "I stopped after reaching the tool-call limit for a single command. "
                    "Any motion stops on its own when its lease expires."
                ),
                "actions": actions,
                "model": self._model,
                "stop_reason": "tool_iteration_limit",
            }
        finally:
            await client.close()

    # ------------------------------------------------------------------
    # Agentic loop internals
    # ------------------------------------------------------------------

    async def _create_message(self, client: Any, convo: list[dict[str, Any]]) -> Any:
        """Call the Messages API once, mapping SDK errors to our error types."""
        try:
            return await client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=_SYSTEM_PROMPT,
                messages=convo,
                tools=self._tool_params,
                thinking={"type": "adaptive"},
            )
        except Exception as exc:
            mapped = _map_provider_error(exc)
            if mapped is None:
                raise
            raise mapped from exc

    def _final_result(self, response: Any, actions: list[dict[str, Any]]) -> dict[str, Any]:
        """Shape the terminal API response into the endpoint's result dict."""
        reply = "".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()
        stop_reason = getattr(response, "stop_reason", None) or "end_turn"
        if not reply:
            if stop_reason == "refusal":
                reply = "The model declined to act on this request."
            else:
                reply = "(no response text)"
        return {
            "reply": reply,
            "actions": actions,
            "model": getattr(response, "model", None) or self._model,
            "stop_reason": stop_reason,
        }

    async def _run_tool_blocks(
        self, content: Any, actions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Execute every tool_use block in *content*, recording actions as we go."""
        results: list[dict[str, Any]] = []
        for block in content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_input = dict(block.input or {})
            ok, payload = await self._execute_tool(block.name, tool_input)
            record: dict[str, Any] = {"tool": block.name, "input": tool_input, "ok": ok}
            if ok:
                record["result"] = payload
            else:
                record["error"] = payload
            actions.append(record)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({"result": payload} if ok else {"error": payload}),
                    "is_error": not ok,
                }
            )
        return results

    async def _execute_tool(self, name: str, tool_input: dict[str, Any]) -> tuple[bool, Any]:
        """Validate and run one tool call; never raises for tool-level failures.

        Returns
        -------
        tuple of (bool, Any)
            ``(True, json-safe result)`` on success, ``(False, error message)``
            on any failure — the model sees the error and can self-correct.
        """
        entry = self._tools.get(name)
        if entry is None:
            return False, f"unknown tool {name!r}"
        args_model, runner = entry
        try:
            args = args_model.model_validate(tool_input)
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in exc.errors()
            )
            return False, f"invalid arguments: {detail}"
        try:
            result = await runner(args)
        except HTTPException as exc:
            logger.warning("AI tool %s failed: %s", name, exc.detail)
            return False, str(exc.detail)
        except ToolExecutionError as exc:
            logger.warning("AI tool %s failed: %s", name, exc)
            return False, str(exc)
        except Exception as exc:
            # Tool boundary: report the failure to the model instead of
            # aborting the whole command (routine conflicts, bad params, ...).
            logger.warning("AI tool %s raised", name, exc_info=True)
            return False, f"{type(exc).__name__}: {exc}"
        return True, _jsonable(result)

    # ------------------------------------------------------------------
    # Tool registry
    # ------------------------------------------------------------------

    def _register(
        self,
        name: str,
        description: str,
        args_model: type[BaseModel],
        runner: _ToolRunner,
    ) -> None:
        """Add one tool to the registry and the Messages API tool params."""
        self._tools[name] = (args_model, runner)
        self._tool_params.append(
            {
                "name": name,
                "description": description,
                "input_schema": args_model.model_json_schema(),
            }
        )

    def _hat_reader(self, method: str) -> _ToolRunner:
        """Build a no-argument runner that calls one read-only HatClient method."""

        async def run(_: BaseModel) -> Any:
            return await self._hat_call(method)

        return run

    def _require_manager(self) -> Any:
        """Return the routine manager or raise a tool-level error."""
        manager = self._get_routine_manager() if self._get_routine_manager is not None else None
        if manager is None:
            raise ToolExecutionError("routine control is not available on this device")
        return manager

    def _register_tools(self) -> None:
        """Register the destructive-free tool surface.

        Deliberately excluded: ``reset_mcu``, every calibration write, raw
        servo pulses, and raw per-motor speeds — chat gets exactly the levers
        the app's manual controls expose, nothing lower-level.
        """

        async def run_drive(args: _DriveArgs) -> Any:
            return await self._hat_call("drive", args.speed_pct, ttl_ms=args.ttl_ms)

        async def run_steer(args: _AngleArgs) -> Any:
            return await self._hat_call("steer", args.angle_deg, ttl_ms=args.ttl_ms)

        async def run_pan(args: _AngleArgs) -> Any:
            return await self._hat_call("pan_camera", args.angle_deg, ttl_ms=args.ttl_ms)

        async def run_tilt(args: _AngleArgs) -> Any:
            return await self._hat_call("tilt_camera", args.angle_deg, ttl_ms=args.ttl_ms)

        async def run_stop(_: _EmptyArgs) -> Any:
            stopped = await self._hat_call("stop_all_motors")
            return {"stopped_channels": stopped}

        async def run_list_routines(_: _EmptyArgs) -> Any:
            if self._get_catalog is None:
                raise ToolExecutionError("routine catalogue is not available on this device")
            return await asyncio.to_thread(self._get_catalog)

        async def run_start_routine(args: _StartRoutineArgs) -> Any:
            manager = self._require_manager()
            return await manager.start(args.routine, args.params)

        async def run_stop_routine(args: _StopRoutineArgs) -> Any:
            manager = self._require_manager()
            info = await manager.stop(args.routine)
            if info is None:
                raise ToolExecutionError(f"routine {args.routine!r} is not running")
            return info

        async def run_stop_all_routines(_: _EmptyArgs) -> Any:
            manager = self._require_manager()
            return {"stopped": await manager.stop_all()}

        self._register(
            "stop",
            "Immediately stop all drive motors. Use whenever the user says stop or you are "
            "unsure motion is safe.",
            _EmptyArgs,
            run_stop,
        )
        self._register(
            "drive",
            "Drive all wheels at a signed speed under a TTL lease; the robot stops "
            "automatically when the lease expires.",
            _DriveArgs,
            run_drive,
        )
        self._register(
            "steer",
            "Set the steering servo angle (0-180 degrees, 90 = straight ahead).",
            _AngleArgs,
            run_steer,
        )
        self._register(
            "pan_camera",
            "Pan the camera head (0-180 degrees, 90 = centre).",
            _AngleArgs,
            run_pan,
        )
        self._register(
            "tilt_camera",
            "Tilt the camera head (0-180 degrees, 90 = level).",
            _AngleArgs,
            run_tilt,
        )
        self._register(
            "read_ultrasonic",
            "Measure the forward obstacle distance in centimetres (roughly 2-400 cm).",
            _EmptyArgs,
            self._hat_reader("read_ultrasonic"),
        )
        self._register(
            "read_grayscale",
            "Read the three downward grayscale sensors as raw 12-bit ADC values "
            "[left, centre, right]; interpretation is robot-specific.",
            _EmptyArgs,
            self._hat_reader("read_grayscale"),
        )
        self._register(
            "get_battery_voltage",
            "Read the battery voltage in volts.",
            _EmptyArgs,
            self._hat_reader("get_battery_voltage"),
        )
        self._register(
            "get_daemon_health",
            "Report the hardware daemon's health, version, and uptime.",
            _EmptyArgs,
            self._hat_reader("health"),
        )
        self._register(
            "get_motor_status",
            "List the active motor TTL leases (empty when the robot is idle).",
            _EmptyArgs,
            self._hat_reader("get_motor_status"),
        )
        self._register(
            "get_servo_status",
            "List the active servo TTL leases.",
            _EmptyArgs,
            self._hat_reader("get_servo_status"),
        )
        self._register(
            "get_mcu_status",
            "Report MCU watchdog reset counters.",
            _EmptyArgs,
            self._hat_reader("get_mcu_status"),
        )
        self._register(
            "list_routines",
            "List the autonomy routines this robot can run, with their parameter schemas.",
            _EmptyArgs,
            run_list_routines,
        )
        self._register(
            "start_routine",
            "Start an autonomy routine by name. The routine runs under a heartbeat lease "
            "maintained by the operator's app and stops automatically if contact lapses.",
            _StartRoutineArgs,
            run_start_routine,
        )
        self._register(
            "stop_routine",
            "Stop one running autonomy routine by name.",
            _StopRoutineArgs,
            run_stop_routine,
        )
        self._register(
            "stop_all_routines",
            "Stop every running autonomy routine.",
            _EmptyArgs,
            run_stop_all_routines,
        )
