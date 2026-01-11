"""Shared session management for Claude SDK clients with message queue support."""

import json
import asyncio
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Any
from enum import Enum

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    SystemMessage,
    ToolUseBlock,
    ToolResultBlock,
)

# Queue configuration
MAX_QUEUE_SIZE = 10


class CancelAction(Enum):
    """Actions to take when a cancel-type message is received."""
    PROCESS_NORMALLY = "process_normally"  # Process this message normally
    SKIP_MESSAGE = "skip_message"  # Skip this message entirely
    AWAIT_CURRENT = "await_current"  # Wait for current processing to finish, then process
    CANCEL_CURRENT = "cancel_current"  # Cancel current processing, then process this


@dataclass
class QueuedMessage:
    """A message waiting in the queue."""
    message_id: str
    content: str
    user_id: str
    session_id: str | None = None


@dataclass
class UserMessageQueue:
    """Per-user message queue with processing state."""
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_QUEUE_SIZE))
    is_processing: bool = False
    current_message_id: str | None = None
    cancel_requested: bool = False
    processor_task: asyncio.Task | None = None
    response_callback: Callable[[dict], Awaitable[None]] | None = None


# Shared session store for all users (web + iOS)
_sessions: dict[str, ClaudeSDKClient] = {}

# Message queues per user
_message_queues: dict[str, UserMessageQueue] = {}

# Persist session_ids to survive restarts
_SESSION_FILE = Path(__file__).parent / ".session_ids.json"
_session_ids: dict[str, str] = {}  # user_id -> session_id


def _load_session_ids():
    """Load persisted session_ids from disk."""
    global _session_ids
    if _SESSION_FILE.exists():
        try:
            _session_ids = json.loads(_SESSION_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            _session_ids = {}


def _save_session_ids():
    """Save session_ids to disk."""
    try:
        _SESSION_FILE.write_text(json.dumps(_session_ids, indent=2))
    except IOError:
        pass


# Load on module import
_load_session_ids()

SYSTEM_PROMPT = "You are a helpful assistant in a terminal-aesthetic chat app called Monios. Keep responses concise and friendly."


async def get_or_create_client(user_id: str) -> ClaudeSDKClient:
    """Get existing client or create new one for user."""
    if user_id not in _sessions:
        options = ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT,
            allowed_tools=[],
            permission_mode="bypassPermissions",
            max_turns=10,  # Allow multiple turns for tool use + response
            cwd="workspace"
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        _sessions[user_id] = client
    return _sessions[user_id]


async def clear_session(user_id: str) -> bool:
    """Clear session for a user. Returns True if session existed."""
    existed = False
    if user_id in _sessions:
        try:
            await _sessions[user_id].disconnect()
        except:
            pass
        del _sessions[user_id]
        existed = True
    if user_id in _session_ids:
        del _session_ids[user_id]
        _save_session_ids()
        existed = True
    return existed


def get_or_create_queue(user_id: str) -> UserMessageQueue:
    """Get existing queue or create new one for user."""
    if user_id not in _message_queues:
        _message_queues[user_id] = UserMessageQueue()
    return _message_queues[user_id]


def should_process_message(
    message: QueuedMessage,
    user_queue: UserMessageQueue
) -> CancelAction:
    """
    Determine what action to take for an incoming message.

    This is the central function for handling cancel/control messages.
    Customize the logic here to decide how to handle special messages.

    Args:
        message: The incoming message to evaluate
        user_queue: The user's queue state (has is_processing, current_message_id, etc.)

    Returns:
        CancelAction indicating what to do with this message
    """
    content_lower = message.content.lower().strip()

    # Check for cancel-type messages
    if content_lower in ("cancel", "/cancel", "stop", "/stop"):
        if user_queue.is_processing:
            # If something is being processed, request cancellation
            return CancelAction.CANCEL_CURRENT
        else:
            # Nothing to cancel, skip this message
            return CancelAction.SKIP_MESSAGE

    # Check for "wait" type messages - wait for current to finish
    if content_lower in ("wait", "/wait"):
        if user_queue.is_processing:
            return CancelAction.AWAIT_CURRENT
        else:
            return CancelAction.SKIP_MESSAGE

    # Check for priority/urgent messages that should interrupt
    if content_lower.startswith("urgent:") or content_lower.startswith("!"):
        if user_queue.is_processing:
            # Cancel current and process this urgent message
            return CancelAction.CANCEL_CURRENT
        else:
            return CancelAction.PROCESS_NORMALLY

    # Default: process normally
    return CancelAction.PROCESS_NORMALLY


async def enqueue_message(
    message_id: str,
    content: str,
    user_id: str,
    session_id: str | None = None
) -> dict:
    """
    Add a message to the user's queue.

    Returns:
        dict with status information about the queued message
    """
    user_queue = get_or_create_queue(user_id)
    queued_msg = QueuedMessage(
        message_id=message_id,
        content=content,
        user_id=user_id,
        session_id=session_id
    )

    # Determine what to do with this message
    action = should_process_message(queued_msg, user_queue)

    if action == CancelAction.SKIP_MESSAGE:
        return {
            "status": "skipped",
            "message_id": message_id,
            "reason": "Message was skipped (nothing to cancel/wait for)"
        }

    if action == CancelAction.CANCEL_CURRENT:
        # Request cancellation of current processing
        user_queue.cancel_requested = True
        # Still queue this message to be processed after cancellation

    # Check if queue is full
    if user_queue.queue.full():
        return {
            "status": "queue_full",
            "message_id": message_id,
            "queue_size": MAX_QUEUE_SIZE,
            "reason": f"Queue is full (max {MAX_QUEUE_SIZE} messages)"
        }

    # Add to queue
    await user_queue.queue.put(queued_msg)

    queue_position = user_queue.queue.qsize()

    return {
        "status": "queued",
        "message_id": message_id,
        "queue_position": queue_position,
        "is_processing": user_queue.is_processing,
        "action": action.value
    }


async def process_queue(user_id: str) -> None:
    """
    Process messages from the user's queue in order.
    This runs as a background task.
    """
    user_queue = get_or_create_queue(user_id)

    while True:
        try:
            # Wait for next message in queue
            queued_msg: QueuedMessage = await user_queue.queue.get()

            # Mark as processing
            user_queue.is_processing = True
            user_queue.current_message_id = queued_msg.message_id
            user_queue.cancel_requested = False

            # Notify client that processing started
            if user_queue.response_callback:
                await user_queue.response_callback({
                    "type": "processing_started",
                    "message_id": queued_msg.message_id,
                    "queue_remaining": user_queue.queue.qsize()
                })

            try:
                # Check if cancellation was requested before we even start
                if user_queue.cancel_requested:
                    if user_queue.response_callback:
                        await user_queue.response_callback({
                            "type": "cancelled",
                            "message_id": queued_msg.message_id,
                            "reason": "Cancelled before processing"
                        })
                    continue

                # Process the message
                response_text, new_session_id, tool_events = await get_response(
                    queued_msg.content,
                    queued_msg.user_id,
                    queued_msg.session_id
                )

                # Check if cancellation was requested during processing
                if user_queue.cancel_requested:
                    if user_queue.response_callback:
                        await user_queue.response_callback({
                            "type": "cancelled",
                            "message_id": queued_msg.message_id,
                            "reason": "Cancelled during processing"
                        })
                    continue

                # Send response back to client
                if user_queue.response_callback:
                    await user_queue.response_callback({
                        "type": "response",
                        "message_id": queued_msg.message_id,
                        "content": response_text,
                        "session_id": new_session_id,
                        "tool_events": tool_events
                    })

            except Exception as e:
                print(f"Error processing message {queued_msg.message_id}: {e}")
                if user_queue.response_callback:
                    await user_queue.response_callback({
                        "type": "error",
                        "message_id": queued_msg.message_id,
                        "error": str(e)
                    })

            finally:
                user_queue.is_processing = False
                user_queue.current_message_id = None
                user_queue.queue.task_done()

        except asyncio.CancelledError:
            print(f"Queue processor for {user_id} cancelled")
            break
        except Exception as e:
            print(f"Queue processor error for {user_id}: {e}")
            await asyncio.sleep(1)  # Prevent tight loop on errors


def start_queue_processor(user_id: str) -> asyncio.Task:
    """Start the queue processor task for a user if not already running."""
    user_queue = get_or_create_queue(user_id)

    if user_queue.processor_task is None or user_queue.processor_task.done():
        user_queue.processor_task = asyncio.create_task(process_queue(user_id))

    return user_queue.processor_task


def set_response_callback(
    user_id: str,
    callback: Callable[[dict], Awaitable[None]] | None
) -> None:
    """Set the callback function for sending responses to the client."""
    user_queue = get_or_create_queue(user_id)
    user_queue.response_callback = callback


def get_queue_status(user_id: str) -> dict:
    """Get the current status of a user's message queue."""
    user_queue = get_or_create_queue(user_id)
    return {
        "queue_size": user_queue.queue.qsize(),
        "max_queue_size": MAX_QUEUE_SIZE,
        "is_processing": user_queue.is_processing,
        "current_message_id": user_queue.current_message_id,
        "cancel_requested": user_queue.cancel_requested
    }


async def get_response(
    message: str, user_id: str, session_id: str | None = None
) -> tuple[str, str | None, list[dict[str, object]]]:
    """Send message and get response for a user."""
    client = await get_or_create_client(user_id)

    # Use provided session_id, or fall back to persisted one
    effective_session_id = session_id or _session_ids.get(user_id)

    print(f"user_id: {user_id}")
    print(f"message: {message}")
    print(f"effective_session_id: {effective_session_id}")
    if effective_session_id:
        await client.query(prompt=message, session_id=effective_session_id)
    else:
        await client.query(prompt=message)

    response_text = ""
    tool_events: list[dict[str, object]] = []
    new_session_id = None
    async for msg in client.receive_response():
        if isinstance(msg, SystemMessage):
            data = msg.data
            new_session_id = data.get("session_id", None)
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    response_text += block.text
                elif isinstance(block, ToolUseBlock):
                    tool_events.append(
                        {
                            "type": "tool_use",
                            "name": block.name,
                            "input": block.input,
                            "tool_use_id": block.id,
                        }
                    )
                elif isinstance(block, ToolResultBlock):
                    tool_events.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": block.is_error,
                        }
                    )

    # Persist the session_id for this user
    if new_session_id:
        _session_ids[user_id] = new_session_id
        _save_session_ids()

    return response_text, new_session_id, tool_events
