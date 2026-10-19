"""Build the minimal Codehead system prompt."""

import platform
from pathlib import Path


def build_system_prompt(workspace: Path) -> str:
    """Build a prompt and append root-level AGENTS.md instructions if present."""
    resolved_workspace = workspace.expanduser().resolve()
    operating_system = platform.system()
    shell = "powershell.exe" if operating_system == "Windows" else "/bin/bash"
    prompt = (
        "You are Codehead, a coding agent.\n"
        f"Work inside this workspace: {resolved_workspace}\n"
        f"Operating system: {operating_system}\n"
        f"Shell: {shell}\n"
        "File paths should be relative to the workspace. Use the available tools "
        "when needed and explain the final result clearly."
    )

    instructions_path = resolved_workspace / "AGENTS.md"
    if not instructions_path.is_file():
        return prompt

    instructions = instructions_path.read_text(encoding="utf-8").strip()
    if not instructions:
        return prompt

    return f"{prompt}\n\nProject instructions from AGENTS.md:\n{instructions}"
