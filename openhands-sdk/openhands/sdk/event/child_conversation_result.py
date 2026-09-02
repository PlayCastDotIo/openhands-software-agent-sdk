"""Event published into a parent conversation when a child thread completes."""

from typing import TYPE_CHECKING, Literal

from pydantic import Field

from openhands.sdk.event.base import Event
from openhands.sdk.event.types import SourceType


if TYPE_CHECKING:
    pass


class ChildConversationResultEvent(Event):
    """A child conversation reached a terminal state.

    Published into the **parent** conversation's event stream so the parent
    (and its WebSocket subscribers — e.g. the agent-canvas frontend) learn a
    child thread finished, without the child having to call back. Mirrors the
    launch-time ``[child-conversation]`` message in the reverse direction.

    Emitted once per child by the agent-server when the child's execution
    status transitions to a terminal state and the child has a
    ``parent_conversation_id``.
    """

    source: SourceType = "environment"

    child_conversation_id: str = Field(
        description="The conversation id of the child that finished."
    )
    parent_conversation_id: str = Field(
        description="The parent conversation the result is published into."
    )
    status: Literal["finished", "error", "stopped"] = Field(
        description="The terminal status the child reached."
    )
    error: str | None = Field(
        default=None,
        description="Present when the child ended in an error/stopped state.",
    )
    url: str | None = Field(
        default=None,
        description="Optional URL to open the finished child conversation.",
    )

    def __str__(self) -> str:
        return (
            f"ChildConversationResult(child={self.child_conversation_id}, "
            f"status={self.status})"
        )
