"""Task tool executor.

This module contains the TaskExecutor class,
which serves as a bridge between the tool interface
and the TaskManager. It translates a TaskAction into
a blocking sub-agent execution and returns a
TaskObservation containing either the task result or an error.
"""

from openhands.sdk.conversation.impl.local_conversation import LocalConversation
from openhands.sdk.logger import get_logger
from openhands.sdk.tool.tool import ToolExecutor
from openhands.tools.task.definition import TaskAction, TaskObservation
from openhands.tools.task.manager import Task, TaskManager, TaskStatus


logger = get_logger(__name__)


class TaskExecutor(ToolExecutor):
    """Executor for the Task tool (blocking only)."""

    def __init__(self, manager: TaskManager):
        self._manager = manager

    def __call__(
        self,
        action: TaskAction,
        conversation: LocalConversation | None = None,
    ) -> TaskObservation:
        try:
            task = self._manager.start_task(
                prompt=action.prompt,
                subagent_type=action.subagent_type,
                description=action.description,
                resume=action.resume,
                llm_profile=action.llm_profile,
                conversation=conversation,
            )
            match task.status:
                case TaskStatus.COMPLETED:
                    return TaskObservation.from_text(
                        text=task.result or "Task completed with no result.",
                        task_id=task.id,
                        subagent=action.subagent_type,
                        status=task.status,
                        conversation_id=self._conversation_id(task),
                        thread=self._thread(task),
                    )
                case TaskStatus.ERROR:
                    return TaskObservation.from_text(
                        text=task.error or "Task failed.",
                        task_id=task.id,
                        subagent=action.subagent_type,
                        status=task.status,
                        is_error=True,
                        conversation_id=self._conversation_id(task),
                        thread=self._thread(task),
                    )
                case _:
                    # this should never happen
                    raise RuntimeError(f"Unknown task status: {task.status}")
        except Exception as e:
            logger.error(f"Task execution failed: {e}", exc_info=True)
            return TaskObservation.from_text(
                text=f"Failed to execute task: {str(e)}",
                task_id="unknown",
                subagent=action.subagent_type,
                status="error",
                is_error=True,
            )

    @staticmethod
    def _conversation_id(task: Task) -> str | None:
        """The sub-agent conversation's id, when the task ran one."""
        if task.conversation is None:
            return None
        return str(task.conversation.state.id)

    @staticmethod
    def _thread(task: Task) -> list[dict[str, str]]:
        """Compact transcript of the sub-agent run, when available."""
        if task.conversation is None:
            return []
        return TaskManager._build_thread(task.conversation)

    def close(self) -> None:
        self._manager.close()
