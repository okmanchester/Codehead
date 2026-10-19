"""Tool-call permission rules and process-local session approvals."""

import fnmatch
import json
import os
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from weakref import WeakKeyDictionary

from codehead.models import ToolCall
from codehead.session import Session

PermissionAction = Literal["allow", "ask", "deny"]
ApprovalChoice = Literal["once", "session", "deny"]

FILE_TOOLS = frozenset({"read", "write", "edit", "glob", "grep"})
READ_TOOLS = frozenset({"read", "glob", "grep"})
SEARCH_TOOLS = frozenset({"glob", "grep"})


@dataclass(frozen=True)
class PermissionRule:
    tool: str
    action: PermissionAction
    path: str | None = None
    command: str | None = None
    workdir: str | None = None


class PermissionPolicy:
    """Evaluate an immutable rule snapshot and remember live-session approvals."""

    def __init__(self, workspace: Path, rules: Iterable[PermissionRule] = ()) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.rules = tuple(rules)
        self._grants: WeakKeyDictionary[Session, set[tuple[str, ...]]] = (
            WeakKeyDictionary()
        )

    @classmethod
    def allow_all(
        cls, workspace: Path, known_tools: Iterable[str]
    ) -> "PermissionPolicy":
        """Grant every known tool without prompting; for unattended runs only."""
        return cls(
            workspace,
            [PermissionRule(tool=name, action="allow") for name in known_tools],
        )

    @classmethod
    def from_file(
        cls, workspace: Path, known_tools: Iterable[str]
    ) -> "PermissionPolicy":
        path = workspace / ".codehead" / "permissions.toml"
        try:
            with path.open("rb") as stream:
                data = tomllib.load(stream)
        except FileNotFoundError:
            return cls(workspace)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(
                f"Cannot load permission rules from {path}: {exc}"
            ) from exc
        if not isinstance(data, dict) or set(data) != {"rules"}:
            raise ValueError("Permission file must contain only [[rules]] entries")
        entries = data["rules"]
        if not isinstance(entries, list):
            raise TypeError("Permission 'rules' must be an array of tables")
        names = set(known_tools)
        rules = []
        for index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                raise TypeError(f"Permission rule {index} must be a table")
            tool = entry.get("tool")
            action = entry.get("action")
            if not isinstance(tool, str) or tool not in names:
                raise ValueError(f"Permission rule {index} has an unknown tool")
            if action not in ("allow", "ask", "deny"):
                raise ValueError(f"Permission rule {index} has an invalid action")
            allowed = {"tool", "action"}
            if tool in FILE_TOOLS:
                allowed.add("path")
            elif tool == "bash":
                allowed.update(("command", "workdir"))
            if set(entry) - allowed:
                raise ValueError(f"Permission rule {index} has unsupported fields")
            for key in allowed - {"tool", "action"}:
                if key in entry and (not isinstance(entry[key], str) or not entry[key]):
                    raise ValueError(f"Permission rule {index} has an invalid {key}")
            for key in ("path", "workdir"):
                if key in entry:
                    pattern = entry[key].replace("\\", "/")
                    if (
                        pattern.startswith("/")
                        or ".." in pattern.split("/")
                        or ":" in pattern
                    ):
                        raise ValueError(
                            f"Permission rule {index} has an invalid {key}"
                        )
                    entry[key] = (
                        "/".join(
                            part for part in pattern.split("/") if part not in ("", ".")
                        )
                        or "."
                    )
            rules.append(PermissionRule(**entry))
        return cls(workspace, rules)

    def action(self, call: ToolCall) -> PermissionAction:
        target = self._target(call)
        for rule in self.rules:
            if rule.tool != call.name:
                continue
            if rule.path is not None and not self._path_rule_matches(
                rule, target[1] if target else None
            ):
                continue
            if rule.command is not None and rule.command != call.arguments.get(
                "command"
            ):
                continue
            if rule.workdir is not None and not self._matches(
                rule.workdir, target[2] if target and call.name == "bash" else None
            ):
                continue
            return rule.action
        return "allow" if call.name in READ_TOOLS else "ask"

    def is_granted(self, session: Session, call: ToolCall) -> bool:
        target = self._target(call)
        return target is not None and target in self._grants.get(session, set())

    def grant(self, session: Session, call: ToolCall) -> None:
        target = self._target(call)
        if target is not None:
            self._grants.setdefault(session, set()).add(target)

    def denial_key(self, call: ToolCall) -> tuple[str, ...]:
        """Use the approved target, or exact arguments for an invalid target."""
        return self._target(call) or (
            call.name,
            "<invalid target>",
            json.dumps(call.arguments, sort_keys=True, ensure_ascii=False),
        )

    def _target(self, call: ToolCall) -> tuple[str, ...] | None:
        if call.name in FILE_TOOLS:
            path = (
                call.arguments.get("path", ".")
                if call.name in SEARCH_TOOLS
                else call.arguments.get("path")
            )
            return self._path_target(call.name, path)
        if call.name == "bash":
            command = call.arguments.get("command")
            workdir = self._path_target("bash", call.arguments.get("workdir", "."))
            if not isinstance(command, str) or not command or workdir is None:
                return None
            return ("bash", command, workdir[1])
        return (
            call.name,
            json.dumps(call.arguments, sort_keys=True, ensure_ascii=False),
        )

    def _path_target(self, tool: str, value: object) -> tuple[str, str] | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = self.workspace / candidate
            resolved = candidate.resolve()
        except (OSError, ValueError, RuntimeError):
            return None
        if not resolved.is_relative_to(self.workspace):
            return None
        relative = resolved.relative_to(self.workspace).as_posix() or "."
        if os.name == "nt":
            relative = relative.casefold()
        return (tool, relative)

    def _path_rule_matches(self, rule: PermissionRule, search_path: str | None) -> bool:
        if search_path is None or rule.path is None:
            return False
        if self._matches(rule.path, search_path):
            return True
        if rule.tool not in SEARCH_TOOLS or rule.action == "allow":
            return False

        # A search can inspect every descendant of its root. Restrictive rules
        # therefore apply whenever their possible subtree overlaps that root.
        pattern = rule.path.replace("\\", "/")
        wildcard = next((i for i, ch in enumerate(pattern) if ch in "*?["), None)
        if wildcard is None:
            literal = pattern
        else:
            literal = pattern[:wildcard].rpartition("/")[0] or "."
        prefix = self._path_target(rule.tool, literal or ".")
        if prefix is None:
            return True
        protected = prefix[1]
        return (
            search_path == "."
            or protected == "."
            or search_path == protected
            or search_path.startswith(protected + "/")
            or protected.startswith(search_path + "/")
        )

    @staticmethod
    def _matches(pattern: str, value: str | None) -> bool:
        if value is None:
            return False
        if os.name == "nt":
            pattern, value = pattern.casefold(), value.casefold()
        return fnmatch.fnmatchcase(value, pattern)
