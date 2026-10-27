"""Server-side conversation state for the AI chat relay (review finding S-24).

``POST /api/ai/command`` used to accept the whole conversation from the app,
including prior ``assistant`` turns, so a client could forge model statements
to steer the next tool call.  The device now keeps each conversation's
plain-text turns itself: the app sends only the new user message plus an
opaque ``conversation_id`` the device issued, and the stored history is the
only source of assistant turns the model ever sees.

State is in memory only — a service restart forgets every conversation, which
the app handles by starting a fresh one.  Conversations expire after a period
of inactivity and the store keeps at most a handful (an owner-only, one-robot
API never needs more), evicting the least recently used.
"""

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

#: Conversation history cap. Dropping whole user/assistant pairs keeps the
#: user-first alternation the Messages API requires.
MAX_HISTORY_MESSAGES = 20

_DEFAULT_IDLE_TTL_S = 1800.0
_DEFAULT_MAX_CONVERSATIONS = 8


def trim_history(
    messages: list[dict[str, str]], max_messages: int = MAX_HISTORY_MESSAGES
) -> list[dict[str, str]]:
    """Cap conversation history, dropping the oldest turns in pairs.

    Parameters
    ----------
    messages : list of dict
        Alternating ``{"role", "content"}`` turns, oldest first.
    max_messages : int
        Maximum number of turns to keep.

    Returns
    -------
    list of dict
        A new list with at most ``max_messages`` turns; the first remaining
        turn keeps the original's role.
    """
    trimmed = list(messages)
    while len(trimmed) > max_messages:
        del trimmed[:2]
    return trimmed


class ConversationNotFoundError(LookupError):
    """The conversation id is unknown — expired, evicted, or never issued."""


class ConversationBusyError(RuntimeError):
    """A command is already running in this conversation."""


@dataclass
class _Conversation:
    messages: list[dict[str, str]] = field(default_factory=list)
    last_used: float = 0.0
    busy: bool = False


class AiConversationStore:
    """In-memory conversation histories keyed by device-issued ids.

    Each command is a ``begin`` → (model run) → ``commit`` or ``abort``
    sequence.  ``begin`` marks the conversation busy so a second command
    cannot interleave turns while the first is still running.  The methods
    contain no ``await`` and are called from the event loop only, so they
    need no lock.

    Parameters
    ----------
    idle_ttl_s : float
        Seconds of inactivity after which a conversation is forgotten.
    max_conversations : int
        Most conversations kept; the least recently used idle one is evicted
        to make room.
    clock : callable, optional
        Monotonic time source (injectable for tests).
    """

    def __init__(
        self,
        idle_ttl_s: float = _DEFAULT_IDLE_TTL_S,
        max_conversations: int = _DEFAULT_MAX_CONVERSATIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._idle_ttl_s = idle_ttl_s
        self._max_conversations = max_conversations
        self._clock = clock
        self._conversations: dict[str, _Conversation] = {}

    def __len__(self) -> int:
        return len(self._conversations)

    def begin(
        self, conversation_id: str | None, user_message: str
    ) -> tuple[str, list[dict[str, str]]]:
        """Start one command and return the history to send to the model.

        Parameters
        ----------
        conversation_id : str or None
            An id from an earlier response, or ``None`` to start a new
            conversation.
        user_message : str
            The new user turn.

        Returns
        -------
        tuple of (str, list of dict)
            The conversation id (new or existing) and the trimmed history
            ending with ``user_message``.

        Raises
        ------
        ConversationNotFoundError
            If ``conversation_id`` is not a live conversation.
        ConversationBusyError
            If a command is already running in it.
        """
        now = self._clock()
        self._expire(now)
        if conversation_id is None:
            self._make_room()
            conversation_id = secrets.token_urlsafe(16)
            conversation = _Conversation()
            self._conversations[conversation_id] = conversation
        else:
            found = self._conversations.get(conversation_id)
            if found is None:
                raise ConversationNotFoundError(conversation_id)
            if found.busy:
                raise ConversationBusyError(conversation_id)
            conversation = found
        conversation.busy = True
        conversation.last_used = now
        history = trim_history([*conversation.messages, {"role": "user", "content": user_message}])
        return conversation_id, history

    def commit(self, conversation_id: str, user_message: str, reply: str) -> None:
        """Record a completed exchange and release the conversation.

        Parameters
        ----------
        conversation_id : str
            The id returned by :meth:`begin`.
        user_message : str
            The user turn passed to :meth:`begin`.
        reply : str
            The model's final reply text.
        """
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            return
        conversation.messages = trim_history(
            [
                *conversation.messages,
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": reply},
            ]
        )
        conversation.busy = False
        conversation.last_used = self._clock()

    def abort(self, conversation_id: str) -> None:
        """Release a conversation after a failed command, recording nothing.

        A conversation that never completed an exchange is dropped, so a
        failed first command does not leave an empty entry behind.

        Parameters
        ----------
        conversation_id : str
            The id returned by :meth:`begin`.
        """
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            return
        if not conversation.messages:
            del self._conversations[conversation_id]
            return
        conversation.busy = False

    def _expire(self, now: float) -> None:
        stale = [
            cid
            for cid, conv in self._conversations.items()
            if not conv.busy and now - conv.last_used > self._idle_ttl_s
        ]
        for cid in stale:
            del self._conversations[cid]

    def _make_room(self) -> None:
        while len(self._conversations) >= self._max_conversations:
            idle = [
                (conv.last_used, cid) for cid, conv in self._conversations.items() if not conv.busy
            ]
            if not idle:
                # Every slot is mid-command; allow a temporary overshoot rather
                # than refusing — each busy entry is released within one run.
                return
            del self._conversations[min(idle)[1]]
