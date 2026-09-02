"""Git show tool: read a file (or list a tree) at a specific git ref."""

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field

from openhands.sdk.git.utils import run_git_command
from openhands.sdk.tool import (
    Action,
    DeclaredResources,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)


if TYPE_CHECKING:
    from openhands.sdk.conversation.state import ConversationState


class GitShowAction(Action):
    """Read a file's content at a specific git ref (commit/branch/tag)."""

    ref: str = Field(
        description=(
            "The git ref to read from, e.g. 'origin/dev', 'origin/webrtcopt', "
            "'HEAD', a commit SHA, or a tag. The file is read as it exists at "
            "this ref — NOT the current working tree."
        )
    )
    path: str = Field(
        description=(
            "Repository-relative path of the file to read (e.g. "
            "'libs/PeerCore/src/x.ts'). Pass '.' or '' to list the tree at the ref."
        )
    )
    offset: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional 0-based line number to start reading from. Use with "
            "'limit' to page through large files."
        ),
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional max number of lines to return. Use with 'offset' to page."
        ),
    )


class GitShowObservation(Observation):
    """Content of a file (or tree listing) at a git ref."""

    ref: str = Field(description="The git ref that was read.")
    path: str = Field(description="The path that was read.")
    file_content: str = Field(
        default="", description="The content read from the file at the ref."
    )
    is_truncated: bool = Field(
        default=False,
        description="Whether the content was truncated due to the limit.",
    )
    lines_shown: tuple[int, int] | None = Field(
        default=None,
        description="If truncated, the 1-indexed range of lines shown.",
    )
    total_lines: int | None = Field(
        default=None, description="Total number of lines in the file."
    )


TOOL_DESCRIPTION = """Read a file's content as it exists at a specific git ref.

Use this when you need the version of a file on a particular branch, commit,
or tag — e.g. the target branch of a PR review — rather than the current
working tree. The `read_file` tool reads the working tree; `git_show` reads
a ref.

Examples:
- Read the dev-branch version of a file:
    git_show(ref="origin/dev", path="libs/PeerCore/src/x.ts")
- Read the PR-branch version:
    git_show(ref="origin/webrtcopt", path="libs/PeerCore/src/x.ts")
- Read at a commit: git_show(ref="abc123def", path="src/main.ts")
- Page through a large file: git_show(ref="HEAD", path="big.ts", offset=100, limit=200)
- List a directory at a ref: git_show(ref="origin/dev", path="libs/PeerCore/src")

Fast, read-only, and parallel-safe. Prefer this over `terminal` + `git show`.
"""

MAX_LINES_PER_READ = 1000


class GitShowTool(ToolDefinition[GitShowAction, GitShowObservation]):
    """Read a file at a git ref without shelling out through the terminal."""

    def declared_resources(self, action: Action) -> DeclaredResources:
        """Read-only, no shared mutable state — safe to run in parallel."""
        assert isinstance(action, GitShowAction)
        return DeclaredResources(keys=(), declared=True)

    @classmethod
    def create(
        cls,
        conv_state: "ConversationState",
    ) -> Sequence["GitShowTool"]:
        """Initialize GitShowTool with the workspace's repo root."""
        working_dir = conv_state.workspace.working_dir
        repo_root = Path(working_dir)
        try:
            repo_root = Path(
                run_git_command(
                    ["git", "--no-pager", "rev-parse", "--show-toplevel"],
                    working_dir,
                ).strip()
            )
        except Exception:
            # Not in a git repo — fall back to the working dir; calls will fail
            # with a clear error.
            pass

        enhanced_description = (
            f"{TOOL_DESCRIPTION}\n\n"
            f"Repository root: {repo_root}\n"
            "Paths are relative to this repository root."
        )

        return [
            cls(
                action_type=GitShowAction,
                observation_type=GitShowObservation,
                description=enhanced_description,
                annotations=ToolAnnotations(
                    title="git_show",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=GitShowExecutor(repo_root),
            )
        ]


class GitShowExecutor(ToolExecutor[GitShowAction, GitShowObservation]):
    """Runs `git show <ref>:<path>` and returns the content, paged."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root

    def __call__(
        self,
        action: GitShowAction,
        conversation=None,  # noqa: ARG002
    ) -> GitShowObservation:
        ref = action.ref.strip()
        path = (action.path or "").strip()
        if not ref:
            return GitShowObservation.from_text(
                text="Error: 'ref' is required.",
                is_error=True,
                ref="",
                path=action.path or "",
                file_content="",
            )

        spec = f"{ref}:{path}" if path and path not in (".", "") else ref
        try:
            output = run_git_command(
                ["git", "--no-pager", "show", spec],
                self.repo_root,
                timeout=30,
            )
        except Exception as e:
            return GitShowObservation.from_text(
                text=f"Error reading {spec}: {e}",
                is_error=True,
                ref=ref,
                path=action.path or "",
                file_content="",
            )

        lines = output.splitlines()
        total = len(lines)
        offset = action.offset or 0
        limit = action.limit
        if offset >= total and total > 0:
            return GitShowObservation.from_text(
                text=f"Error: Offset {offset} is beyond file length ({total} lines)",
                is_error=True,
                ref=ref,
                path=action.path or "",
                file_content="",
            )
        end = min(offset + (limit or MAX_LINES_PER_READ), total)
        shown = lines[offset:end]
        content = "\n".join(
            f"{i:6d}  {ln}" for i, ln in enumerate(shown, start=offset + 1)
        )
        is_truncated = end < total
        lines_shown = (offset + 1, end) if is_truncated else None

        agent_obs = [f"Read {spec}"]
        if is_truncated:
            agent_obs.append(f"(showing lines {offset + 1}-{end} of {total})")
            agent_obs.append(
                f"To read more, use git_show(ref='{ref}', path='{action.path or ''}', "
                f"offset={end}, limit={action.limit or MAX_LINES_PER_READ})"
            )
        return GitShowObservation.from_text(
            text=" ".join(agent_obs) + "\n\n" + content,
            ref=ref,
            path=action.path or "",
            file_content=content,
            is_truncated=is_truncated,
            lines_shown=lines_shown,
            total_lines=total if not is_truncated else None,
        )


# Automatically register when this module is imported.
register_tool(GitShowTool.name, GitShowTool)
