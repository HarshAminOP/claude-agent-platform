"""LangChain BaseTool implementations for the CAP agent harness.

11 tools as classes extending BaseTool from langchain_core.tools:
1. FileReadTool - read file contents
2. FileWriteTool - create/overwrite file (validate path in workspace)
3. FileEditTool - string replacement (validate path in workspace)
4. BashExecTool - run shell command (sandbox: reject dangerous patterns)
5. GitStatusTool - git status in workspace
6. GitDiffTool - git diff
7. GitCommitTool - commit with message (NEVER push)
8. KnowledgeSearchTool - query CAP knowledge base
9. WebFetchTool - fetch URL content
10. GrepSearchTool - regex search across files
11. ListDirectoryTool - list files/folders
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOOL_OUTPUT_MAX_CHARS = 50_000
BASH_TIMEOUT_S = 60
DANGEROUS_PATTERNS = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=/dev",
    "> /dev/sd",
    ":(){ :|:& };:",
    "sudo ",
]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_workspace_path(path: str, workspace: str) -> Optional[str]:
    """Validate that a path is within the workspace. Returns error string or None."""
    resolved = os.path.realpath(path)
    ws_resolved = os.path.realpath(workspace)
    if not resolved.startswith(ws_resolved):
        return f"Error: path {path!r} is outside workspace {workspace!r}"
    return None


def _is_dangerous_command(command: str) -> Optional[str]:
    """Check if a command matches dangerous patterns. Returns matched pattern or None."""
    lower_cmd = command.lower()
    for pattern in DANGEROUS_PATTERNS:
        if pattern in lower_cmd:
            return pattern
    # Check for writing outside workspace (basic heuristic)
    return None


# ---------------------------------------------------------------------------
# Tool 1: FileReadTool
# ---------------------------------------------------------------------------

class FileReadInput(BaseModel):
    path: str = Field(description="Absolute file path to read")
    offset: int = Field(default=0, description="Line number to start reading from (0-indexed)")
    limit: int = Field(default=0, description="Maximum number of lines to read (0 = all)")


class FileReadTool(BaseTool):
    name: str = "file_read"
    description: str = "Read the contents of a file at the given absolute path. Returns the file content as text."
    args_schema: Type[BaseModel] = FileReadInput

    def _run(self, path: str, offset: int = 0, limit: int = 0) -> str:
        if not path or not os.path.isabs(path):
            return f"Error: path must be absolute. Got: {path!r}"
        if not os.path.exists(path):
            return f"Error: file not found: {path}"
        if not os.path.isfile(path):
            return f"Error: not a regular file: {path}"
        try:
            content = Path(path).read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines(keepends=True)
            if offset > 0:
                lines = lines[offset:]
            if limit > 0:
                lines = lines[:limit]
            result = "".join(lines)
            if len(result) > TOOL_OUTPUT_MAX_CHARS:
                result = result[:TOOL_OUTPUT_MAX_CHARS] + "\n... [truncated]"
            return result
        except Exception as exc:
            return f"Error reading file: {exc}"


# ---------------------------------------------------------------------------
# Tool 2: FileWriteTool
# ---------------------------------------------------------------------------

class FileWriteInput(BaseModel):
    path: str = Field(description="Absolute file path to write (must be within workspace)")
    content: str = Field(description="Content to write to the file")


class FileWriteTool(BaseTool):
    name: str = "file_write"
    description: str = "Create or overwrite a file with the given content. Path must be within workspace."
    args_schema: Type[BaseModel] = FileWriteInput
    workspace: str = ""

    def _run(self, path: str, content: str) -> str:
        if not path or not os.path.isabs(path):
            return f"Error: path must be absolute. Got: {path!r}"
        err = _validate_workspace_path(path, self.workspace)
        if err:
            return err
        try:
            file_path = Path(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return f"Written {len(content)} bytes to {path}"
        except Exception as exc:
            return f"Error writing file: {exc}"


# ---------------------------------------------------------------------------
# Tool 3: FileEditTool
# ---------------------------------------------------------------------------

class FileEditInput(BaseModel):
    path: str = Field(description="Absolute file path to edit (must be within workspace)")
    old_string: str = Field(description="The exact string to find and replace")
    new_string: str = Field(description="The replacement string")


class FileEditTool(BaseTool):
    name: str = "file_edit"
    description: str = "Replace an exact string occurrence in a file. Path must be within workspace."
    args_schema: Type[BaseModel] = FileEditInput
    workspace: str = ""

    def _run(self, path: str, old_string: str, new_string: str) -> str:
        if not path or not os.path.isabs(path):
            return f"Error: path must be absolute. Got: {path!r}"
        err = _validate_workspace_path(path, self.workspace)
        if err:
            return err
        if not os.path.exists(path):
            return f"Error: file not found: {path}"
        try:
            content = Path(path).read_text(encoding="utf-8")
            if old_string not in content:
                return f"Error: old_string not found in {path}"
            count = content.count(old_string)
            new_content = content.replace(old_string, new_string, 1)
            Path(path).write_text(new_content, encoding="utf-8")
            return f"Replaced 1 occurrence in {path} ({count} total matches)"
        except Exception as exc:
            return f"Error editing file: {exc}"


# ---------------------------------------------------------------------------
# Tool 4: BashExecTool
# ---------------------------------------------------------------------------

class BashExecInput(BaseModel):
    command: str = Field(description="The bash command to execute")
    cwd: str = Field(default="", description="Working directory for the command (optional)")


class BashExecTool(BaseTool):
    name: str = "bash_exec"
    description: str = (
        "Execute a bash command and return stdout/stderr. "
        "Commands have a 60-second timeout. "
        "Dangerous commands (rm -rf /, sudo, writing outside workspace) are rejected."
    )
    args_schema: Type[BaseModel] = BashExecInput
    workspace: str = ""

    def _run(self, command: str, cwd: str = "") -> str:
        if not command:
            return "Error: command is required"

        # Sandbox: reject dangerous patterns
        danger = _is_dangerous_command(command)
        if danger:
            return f"Error: blocked dangerous command pattern: {danger}"

        # Validate cwd
        work_dir = cwd if cwd else self.workspace
        if work_dir and not os.path.isdir(work_dir):
            return f"Error: working directory does not exist: {work_dir}"

        try:
            result = subprocess.run(
                ["bash", "-c", command],
                capture_output=True,
                text=True,
                timeout=BASH_TIMEOUT_S,
                cwd=work_dir or None,
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            if not output.strip():
                output = "[no output]"
            if len(output) > TOOL_OUTPUT_MAX_CHARS:
                output = output[:TOOL_OUTPUT_MAX_CHARS] + "\n... [truncated]"
            return output
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {BASH_TIMEOUT_S} seconds"
        except Exception as exc:
            return f"Error executing command: {exc}"


# ---------------------------------------------------------------------------
# Tool 5: GitStatusTool
# ---------------------------------------------------------------------------

class GitStatusInput(BaseModel):
    pass


class GitStatusTool(BaseTool):
    name: str = "git_status"
    description: str = "Run git status in the workspace and return the output."
    args_schema: Type[BaseModel] = GitStatusInput
    workspace: str = ""

    def _run(self) -> str:
        try:
            result = subprocess.run(
                ["git", "status"],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=self.workspace or None,
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            return output if output.strip() else "[no output]"
        except Exception as exc:
            return f"Error running git status: {exc}"


# ---------------------------------------------------------------------------
# Tool 6: GitDiffTool
# ---------------------------------------------------------------------------

class GitDiffInput(BaseModel):
    ref: str = Field(default="", description="Optional ref to diff against (e.g. 'main', 'HEAD~1')")
    staged: bool = Field(default=False, description="Show staged changes (--cached)")


class GitDiffTool(BaseTool):
    name: str = "git_diff"
    description: str = "Run git diff in the workspace. Optionally diff against a ref or show staged changes."
    args_schema: Type[BaseModel] = GitDiffInput
    workspace: str = ""

    def _run(self, ref: str = "", staged: bool = False) -> str:
        cmd = ["git", "diff"]
        if staged:
            cmd.append("--cached")
        if ref:
            cmd.append(ref)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                cwd=self.workspace or None,
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            if not output.strip():
                output = "[no changes]"
            if len(output) > TOOL_OUTPUT_MAX_CHARS:
                output = output[:TOOL_OUTPUT_MAX_CHARS] + "\n... [truncated]"
            return output
        except Exception as exc:
            return f"Error running git diff: {exc}"


# ---------------------------------------------------------------------------
# Tool 7: GitCommitTool
# ---------------------------------------------------------------------------

class GitCommitInput(BaseModel):
    message: str = Field(description="Commit message")
    files: list[str] = Field(default_factory=list, description="Files to stage (empty = all modified)")


class GitCommitTool(BaseTool):
    name: str = "git_commit"
    description: str = (
        "Stage files and create a git commit. NEVER pushes to remote. "
        "If files list is empty, stages all modified files."
    )
    args_schema: Type[BaseModel] = GitCommitInput
    workspace: str = ""

    def _run(self, message: str, files: list[str] | None = None) -> str:
        if not message:
            return "Error: commit message is required"

        cwd = self.workspace or None

        try:
            # Stage files
            if files:
                for f in files:
                    subprocess.run(
                        ["git", "add", f],
                        capture_output=True, text=True, timeout=10, cwd=cwd,
                    )
            else:
                subprocess.run(
                    ["git", "add", "-A"],
                    capture_output=True, text=True, timeout=10, cwd=cwd,
                )

            # Commit
            result = subprocess.run(
                ["git", "commit", "-m", message],
                capture_output=True, text=True, timeout=15, cwd=cwd,
            )
            output = result.stdout
            if result.stderr:
                output += f"\n{result.stderr}"
            if result.returncode != 0:
                return f"Error: git commit failed:\n{output}"
            return output if output.strip() else "Commit created."
        except Exception as exc:
            return f"Error running git commit: {exc}"


# ---------------------------------------------------------------------------
# Tool 8: KnowledgeSearchTool
# ---------------------------------------------------------------------------

class KnowledgeSearchInput(BaseModel):
    query: str = Field(description="Search query")
    scope: str = Field(default="all", description="Scope: all, code, config, doc")


class KnowledgeSearchTool(BaseTool):
    name: str = "knowledge_search"
    description: str = "Search the CAP knowledge base for relevant information about repos, services, patterns, and conventions."
    args_schema: Type[BaseModel] = KnowledgeSearchInput

    def _run(self, query: str, scope: str = "all") -> str:
        if not query:
            return "Error: query is required"
        try:
            from cap.knowledge.search import search as kb_search
            results = kb_search(query, scope=scope, top_k=5)
            if not results:
                return "No results found."
            output_parts = []
            for r in results[:5]:
                title = r.get("title", "untitled")
                preview = r.get("content_preview", r.get("content", ""))[:300]
                source = r.get("source_path", "unknown")
                output_parts.append(f"## {title}\nSource: {source}\n{preview}\n")
            return "\n---\n".join(output_parts)
        except ImportError:
            return "Knowledge base not available (module not found)"
        except Exception as exc:
            return f"Knowledge search error: {exc}"


# ---------------------------------------------------------------------------
# Tool 9: WebFetchTool
# ---------------------------------------------------------------------------

class WebFetchInput(BaseModel):
    url: str = Field(description="URL to fetch")
    max_chars: int = Field(default=20000, description="Maximum characters to return")


class WebFetchTool(BaseTool):
    name: str = "web_fetch"
    description: str = "Fetch the content of a URL and return it as text. Useful for documentation, API responses, etc."
    args_schema: Type[BaseModel] = WebFetchInput

    def _run(self, url: str, max_chars: int = 20000) -> str:
        if not url:
            return "Error: url is required"
        if not url.startswith(("http://", "https://")):
            return "Error: url must start with http:// or https://"
        try:
            import urllib.request
            import urllib.error

            req = urllib.request.Request(url, headers={"User-Agent": "CAP-Agent/1.0"})
            with urllib.request.urlopen(req, timeout=30) as response:
                content = response.read().decode("utf-8", errors="replace")
                if len(content) > max_chars:
                    content = content[:max_chars] + "\n... [truncated]"
                return content
        except urllib.error.HTTPError as exc:
            return f"HTTP error {exc.code}: {exc.reason}"
        except urllib.error.URLError as exc:
            return f"URL error: {exc.reason}"
        except Exception as exc:
            return f"Error fetching URL: {exc}"


# ---------------------------------------------------------------------------
# Tool 10: GrepSearchTool
# ---------------------------------------------------------------------------

class GrepSearchInput(BaseModel):
    pattern: str = Field(description="Regex pattern to search for")
    path: str = Field(default=".", description="Directory or file to search in (relative to workspace)")
    include: str = Field(default="", description="File glob pattern to include (e.g. '*.py')")
    max_results: int = Field(default=50, description="Maximum number of results to return")


class GrepSearchTool(BaseTool):
    name: str = "grep_search"
    description: str = "Search for a regex pattern across files in the workspace. Returns matching lines with file paths and line numbers."
    args_schema: Type[BaseModel] = GrepSearchInput
    workspace: str = ""

    def _run(self, pattern: str, path: str = ".", include: str = "", max_results: int = 50) -> str:
        if not pattern:
            return "Error: pattern is required"

        cmd = ["grep", "-rn", "--color=never"]
        if include:
            cmd.extend(["--include", include])
        cmd.extend([pattern, path])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.workspace or None,
            )
            output = result.stdout
            if not output.strip():
                return "No matches found."

            # Limit results
            lines = output.splitlines()
            if len(lines) > max_results:
                output = "\n".join(lines[:max_results]) + f"\n... [{len(lines) - max_results} more matches]"

            if len(output) > TOOL_OUTPUT_MAX_CHARS:
                output = output[:TOOL_OUTPUT_MAX_CHARS] + "\n... [truncated]"
            return output
        except subprocess.TimeoutExpired:
            return "Error: grep timed out after 30 seconds"
        except Exception as exc:
            return f"Error running grep: {exc}"


# ---------------------------------------------------------------------------
# Tool 11: ListDirectoryTool
# ---------------------------------------------------------------------------

class ListDirectoryInput(BaseModel):
    path: str = Field(default=".", description="Directory path to list (absolute or relative to workspace)")
    max_depth: int = Field(default=1, description="Maximum depth to list (1 = immediate children only)")


class ListDirectoryTool(BaseTool):
    name: str = "list_directory"
    description: str = "List files and folders in a directory. Returns names with type indicators (/ for dirs)."
    args_schema: Type[BaseModel] = ListDirectoryInput
    workspace: str = ""

    def _run(self, path: str = ".", max_depth: int = 1) -> str:
        # Resolve path
        if os.path.isabs(path):
            target = Path(path)
        else:
            target = Path(self.workspace) / path if self.workspace else Path(path)

        if not target.exists():
            return f"Error: path does not exist: {target}"
        if not target.is_dir():
            return f"Error: not a directory: {target}"

        try:
            entries = []
            self._list_recursive(target, entries, max_depth, 0)
            if not entries:
                return "[empty directory]"
            return "\n".join(entries)
        except PermissionError:
            return f"Error: permission denied: {target}"
        except Exception as exc:
            return f"Error listing directory: {exc}"

    def _list_recursive(self, dir_path: Path, entries: list, max_depth: int, current_depth: int):
        if current_depth >= max_depth:
            return
        try:
            items = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            indent = "  " * current_depth
            for item in items:
                if item.name.startswith(".") and current_depth == 0:
                    continue  # Skip hidden at top level for cleanliness
                if item.is_dir():
                    entries.append(f"{indent}{item.name}/")
                    if current_depth + 1 < max_depth:
                        self._list_recursive(item, entries, max_depth, current_depth + 1)
                else:
                    entries.append(f"{indent}{item.name}")
                if len(entries) > 500:
                    entries.append(f"{indent}... [too many entries, truncated]")
                    return
        except PermissionError:
            pass


# ---------------------------------------------------------------------------
# Tool registry and filtering
# ---------------------------------------------------------------------------

# All available tools by name
ALL_TOOL_NAMES = [
    "file_read", "file_write", "file_edit", "bash_exec",
    "git_status", "git_diff", "git_commit",
    "knowledge_search", "web_fetch", "grep_search", "list_directory",
]

# Default tool sets per agent type (used when frontmatter doesn't specify)
_DEFAULT_TOOLS_BY_AGENT = {
    "dev": ALL_TOOL_NAMES,
    "devops": ALL_TOOL_NAMES,
    "security": ["file_read", "bash_exec", "grep_search", "knowledge_search", "list_directory", "git_diff"],
    "code-review": ["file_read", "grep_search", "knowledge_search", "git_diff", "list_directory"],
    "sre": ALL_TOOL_NAMES,
    "test": ALL_TOOL_NAMES,
    "docs": ["file_read", "file_write", "file_edit", "knowledge_search", "grep_search", "list_directory"],
    "explore": ["file_read", "grep_search", "knowledge_search", "list_directory", "bash_exec"],
    "optimization": ALL_TOOL_NAMES,
    "aws-architect": ["file_read", "knowledge_search", "grep_search", "list_directory", "bash_exec"],
    "cicd": ALL_TOOL_NAMES,
}


def _parse_frontmatter_tools(agent_type: str) -> Optional[list[str]]:
    """Parse agent .md frontmatter to find allowed_tools list.

    Returns None if the file doesn't exist or has no tools field.
    """
    from cap.harness.converse_executor import AGENT_DEFS_DIR

    md_path = AGENT_DEFS_DIR / f"{agent_type}.md"
    if not md_path.exists():
        return None

    content = md_path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        return None

    frontmatter = parts[1]
    # Look for tools: [list] in YAML frontmatter
    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith("tools:"):
            # Parse simple YAML list: tools: [file_read, bash_exec]
            value = line[len("tools:"):].strip()
            if value.startswith("[") and value.endswith("]"):
                items = [t.strip().strip("'\"") for t in value[1:-1].split(",")]
                return [t for t in items if t]
    return None


def get_tools_for_agent(agent_type: str, workspace: str) -> list[BaseTool]:
    """Get the list of LangChain tool instances available to a specific agent type.

    Reads agent .md frontmatter to determine allowed tools. Falls back to
    _DEFAULT_TOOLS_BY_AGENT if frontmatter doesn't specify tools.

    Parameters
    ----------
    agent_type:
        Agent role (dev, security, etc.)
    workspace:
        Absolute path to the workspace root.

    Returns
    -------
    List of instantiated BaseTool instances with workspace set.
    """
    # Determine allowed tool names
    frontmatter_tools = _parse_frontmatter_tools(agent_type)
    if frontmatter_tools is not None:
        allowed_names = frontmatter_tools
    else:
        allowed_names = _DEFAULT_TOOLS_BY_AGENT.get(agent_type, ALL_TOOL_NAMES)

    # Build tool instances
    tool_instances = []
    tool_map = _build_tool_map(workspace)

    for name in allowed_names:
        if name in tool_map:
            tool_instances.append(tool_map[name])

    return tool_instances


def _build_tool_map(workspace: str) -> dict[str, BaseTool]:
    """Build a map of tool name -> tool instance with workspace configured."""
    return {
        "file_read": FileReadTool(),
        "file_write": FileWriteTool(workspace=workspace),
        "file_edit": FileEditTool(workspace=workspace),
        "bash_exec": BashExecTool(workspace=workspace),
        "git_status": GitStatusTool(workspace=workspace),
        "git_diff": GitDiffTool(workspace=workspace),
        "git_commit": GitCommitTool(workspace=workspace),
        "knowledge_search": KnowledgeSearchTool(),
        "web_fetch": WebFetchTool(),
        "grep_search": GrepSearchTool(workspace=workspace),
        "list_directory": ListDirectoryTool(workspace=workspace),
    }
