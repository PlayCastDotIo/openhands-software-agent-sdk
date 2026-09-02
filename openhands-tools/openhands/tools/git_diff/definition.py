"""Git diff tool: get a diff between two refs (or working tree vs a ref)."""

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


class GitDiffAction(Action):
    """Get a git diff between two refs, or the working tree vs a ref."""

    ref: str | None = Field(
        default=None,
        description=(
            "The ref to diff against, e.g. 'origin/dev', 'HEAD', a commit SHA. "
            "When omitted, diffs the working tree against the index (like "
            "'git diff'). To compare two branches use 'ref_a...ref_b' in a "
            "single string, e.g. 'origin/dev...origin/webrtcopt'."
        ),
    )
    ref_a: str | None = Field(
        default=None,
        description=(
            "First ref of a range (alternative to the 'ref' form). When both "
            "'ref_a' and 'ref_b' are set, diffs 'ref_a...ref_b'."
        ),
    )
    ref_b: str | None = Field(
        default=None,
        description="Second ref of a range (must be set with 'ref_a').",
    )
    path: str | None = Field(
        default=None,
        description=(
            "Optional repo-relative path to restrict the diff to one file or "
            "directory. When omitted, diffs the whole repository."
        ),
    )
    stat: bool = Field(
        default=False,
        description=(
            "When true, return the diffstat (per-file +/- line counts) instead "
            "of the full patch. Use for a quick overview of what changed."
        ),
    )
    max_lines: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional cap on the number of lines of diff returned (the diff "
            "can be large). When omitted, defaults to a generous cap."
        ),
    )


class GitDiffObservation(Observation):
    """Diff output between two refs."""

    ref: str = Field(description="The effective diff spec that was run.")
    diff: str = Field(default="", description="The diff output (patch or stat).")
    is_truncated: bool = Field(
        default=False,
        description="Whether the diff was truncated by max_lines.",
    )


TOOL_DESCRIPTION = """Get a git diff between two refs, or the working tree vs a ref.

Use this for PR/branch review when you need to see what changed. Returns the
patch (or a per-file stat) directly, without a terminal round-trip.

Examples:
- Working tree vs HEAD: git_diff(ref="HEAD")
- Two branches (PR review): git_diff(ref="origin/dev...origin/webrtcopt")
- Two branches, one file: git_diff(ref="origin/dev...origin/webrtcopt", path="apps/realtime-api/src/signalling.ts")
- Stat overview: git_diff(ref="origin/dev...origin/webrtcopt", stat=true)
- Range form: git_diff(ref_a="origin/dev", ref_b="origin/webrtcopt")
- Working tree changes: git_diff()  (ref omitted)

Fast, read-only, and parallel-safe. Prefer this over `terminal` + `git diff`.
"""

DEFAULT_MAX_DIFF_LINES = 500


class GitDiffTool(ToolDefinition[GitDiffAction, GitDiffObservation]):
    """Get a diff between refs without shelling out through the terminal."""

    def declared_resources(self, action: Action) -> DeclaredResources:
        """Read-only, no shared mutable state — safe to run in parallel."""
        assert isinstance(action, GitDiffAction)
        return DeclaredResources(keys=(), declared=True)

    @classmethod
    def create(
        cls,
        conv_state: "ConversationState",
    ) -> Sequence["GitDiffTool"]:
        """Initialize GitDiffTool with the workspace's repo root."""
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
            pass

        enhanced_description = (
            f"{TOOL_DESCRIPTION}\n\n"
            f"Repository root: {repo_root}\n"
            "Paths are relative to this repository root."
        )

        return [
            cls(
                action_type=GitDiffAction,
                observation_type=GitDiffObservation,
                description=enhanced_description,
                annotations=ToolAnnotations(
                    title="git_diff",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=GitDiffExecutor(repo_root),
            )
        ]


class GitDiffExecutor(ToolExecutor[GitDiffAction, GitDiffObservation]):
    """Runs `git diff <spec>` and returns the output, capped."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root

    def __call__(
        self,
        action: GitDiffAction,
        conversation=None,  # noqa: ARG002
    ) -> GitDiffObservation:
        if action.ref_a or action.ref_b:
            if not (action.ref_a and action.ref_b):
                return GitDiffObservation.from_text(
                    text="Error: ref_a and ref_b must both be set, or use 'ref'.",
                    is_error=True,
                    ref="",
                    diff="",
                )
            spec = f"{action.ref_a.strip()}...{action.ref_b.strip()}"
        else:
            spec = (action.ref or "").strip()

        args = ["git", "--no-pager", "diff"]
        if action.stat:
            args.append("--stat")
        if spec:
            args.append(spec)
        if action.path and action.path not in (".", ""):
            args.append("--")
            args.append(action.path)

        try:
            output = run_git_command(args, self.repo_root, timeout=60)
        except Exception as e:
            return GitDiffObservation.from_text(
                text=f"Error running git diff: {e}",
                is_error=True,
                ref=spec,
                diff="",
            )

        max_lines = action.max_lines or DEFAULT_MAX_DIFF_LINES
        lines = output.splitlines()
        if len(lines) > max_lines:
            shown = lines[:max_lines]
            is_truncated = True
            text = "\n".join(shown) + (
                f"\n\n[Diff truncated: showing first {max_lines} of "
                f"{len(lines)} lines. Use 'path' to narrow, or set a larger "
                f"'max_lines'.]"
            )
        else:
            text = output
            is_truncated = False

        return GitDiffObservation.from_text(
            text=text,
            ref=spec or "(working tree vs index)",
            diff=text,
            is_truncated=is_truncated,
        )


# Automatically register when this module is imported.
register_tool(GitDiffTool.name, GitDiffTool)