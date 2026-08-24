import re
from dataclasses import dataclass
from typing import Any

from .models import ChangedLine


@dataclass
class ParsedDiff:
    files: list[str]
    added_lines: list[ChangedLine]

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": list(self.files),
            "added_lines": [
                {"path": item.path, "line": item.line, "content": item.content}
                for item in self.added_lines
            ],
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ParsedDiff":
        if not isinstance(value, dict):
            raise ValueError("parsed diff must be an object")
        files = value.get("files")
        if (
            not isinstance(files, list)
            or any(not isinstance(path, str) for path in files)
            or len(files) != len(set(files))
        ):
            raise ValueError("parsed diff files must be a unique list of strings")
        raw_lines = value.get("added_lines")
        if not isinstance(raw_lines, list):
            raise ValueError("parsed diff added_lines must be a list")
        added_lines = []
        for raw in raw_lines:
            if not isinstance(raw, dict):
                raise ValueError("parsed diff added lines must be objects")
            path = raw.get("path")
            line = raw.get("line")
            content = raw.get("content")
            if not isinstance(path, str) or not isinstance(content, str):
                raise ValueError("parsed diff line path and content must be strings")
            if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
                raise ValueError("parsed diff line number must be a positive integer")
            added_lines.append(ChangedLine(path, line, content))
        return cls(list(files), added_lines)


HUNK = re.compile(r"^@@ -(?:\d+)(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(diff: str) -> ParsedDiff:
    files: list[str] = []
    seen_files: set[str] = set()
    added: list[ChangedLine] = []
    current_path = ""
    new_line = 0
    remaining_new_lines = 0
    in_hunk = False

    for raw in diff.splitlines():
        match = HUNK.match(raw)
        if match:
            new_line = int(match.group(1))
            remaining_new_lines = int(match.group(2) or 1)
            in_hunk = remaining_new_lines > 0
            continue
        if in_hunk:
            if raw.startswith("+"):
                added.append(ChangedLine(current_path or "unknown", new_line, raw[1:]))
                new_line += 1
                remaining_new_lines -= 1
            elif raw.startswith("-"):
                continue
            elif raw.startswith("\\ No newline"):
                continue
            else:
                new_line += 1
                remaining_new_lines -= 1
            if remaining_new_lines == 0:
                in_hunk = False
            continue
        if raw.startswith("+++ "):
            current_path = raw[4:].strip()
            if current_path.startswith("b/"):
                current_path = current_path[2:]
            if current_path != "/dev/null" and current_path not in seen_files:
                seen_files.add(current_path)
                files.append(current_path)
            in_hunk = False
            continue
    return ParsedDiff(files=files, added_lines=added)
