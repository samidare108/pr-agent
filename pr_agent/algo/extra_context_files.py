"""
Load additional repository files into the PR review prompt context.

Supports:
- Manual paths from config ``extra_context_files``
- Auto-discovery of PHP require/include targets from added diff lines (+)
"""

from __future__ import annotations

import posixpath
import re
from typing import Callable, Dict, Iterable, List, Optional, Set

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

# require_once 'path.php' | require("path.php") | include_once(...) | include(...)
PHP_INCLUDE_RE = re.compile(
    r"""(?P<kw>require_once|require|include_once|include)\s*
        (?:\(\s*)?
        (?P<quote>['"])(?P<path>(?:\\.|(?!\2).)+?)\2
        \s*\)?\s*;?
    """,
    re.IGNORECASE | re.VERBOSE,
)

_EXTRA_CONTEXT_HEADER = "## Related context file: '{filename}'"
_EXTRA_CONTEXT_NOTE = "(not part of the PR diff; included for review context)"


def _decode_legacy_bytes(content):
    from pr_agent.algo.legacy_encoding_diff import decode_legacy_bytes

    return decode_legacy_bytes(content)


def extra_context_files_enabled() -> bool:
    if get_manual_extra_context_paths():
        return True
    if not (
        get_settings().config.get("enable_extra_context_files", False)
        or get_settings().config.get("legacy_encoding_repair", False)
    ):
        return False
    return auto_php_include_context_enabled()


def auto_php_include_context_enabled() -> bool:
    if not bool(get_settings().config.get("auto_php_include_context", True)):
        return False
    return bool(
        get_settings().config.get("legacy_encoding_repair", False)
        or get_settings().config.get("enable_extra_context_files", False)
    )


def get_manual_extra_context_paths() -> List[str]:
    paths = get_settings().config.get("extra_context_files", [])
    if isinstance(paths, str):
        paths = [p.strip() for p in paths.split(",") if p.strip()]
    return [p.replace("\\", "/").strip() for p in paths if p and str(p).strip()]


def get_max_extra_context_files() -> int:
    return int(get_settings().config.get("max_extra_context_files", 10) or 10)


def _iter_added_patch_lines(patch: Optional[str]) -> Iterable[str]:
    if not patch:
        return
    for line in patch.splitlines():
        if line.startswith("+++") or not line.startswith("+"):
            continue
        yield line[1:]


def is_safe_repo_relative_path(path: str) -> bool:
    normalized = posixpath.normpath(path.replace("\\", "/"))
    if normalized in (".", ""):
        return False
    if normalized.startswith("../") or normalized == "..":
        return False
    if normalized.startswith("/"):
        return False
    return True


def resolve_include_path_candidates(source_file: str, include_path: str) -> List[str]:
    """Return repo-relative path candidates for a PHP include string."""
    include_path = include_path.strip().replace("\\", "/")
    if not include_path:
        return []

    candidates: List[str] = []
    seen: Set[str] = set()

    def add(candidate: Optional[str]) -> None:
        if not candidate:
            return
        norm = posixpath.normpath(candidate.replace("\\", "/"))
        if not is_safe_repo_relative_path(norm):
            return
        if norm not in seen:
            seen.add(norm)
            candidates.append(norm)

    add(include_path)

    source_dir = posixpath.dirname(source_file.replace("\\", "/"))
    if source_dir:
        add(posixpath.join(source_dir, include_path))
    else:
        add(include_path)

    return candidates


def extract_php_include_paths_from_diff_files(diff_files: List[FilePatchInfo]) -> Set[str]:
    discovered: Set[str] = set()
    for file in diff_files:
        if file.edit_type == EDIT_TYPE.DELETED:
            continue
        for line in _iter_added_patch_lines(file.patch):
            for match in PHP_INCLUDE_RE.finditer(line):
                for candidate in resolve_include_path_candidates(file.filename, match.group("path")):
                    discovered.add(candidate)
    return discovered


def collect_extra_context_paths(diff_files: List[FilePatchInfo]) -> List[str]:
    """Merge manual config paths and auto-discovered PHP include paths."""
    paths: List[str] = []
    seen: Set[str] = set()
    pr_filenames = {f.filename.strip() for f in diff_files}

    def add(path: str) -> None:
        norm = posixpath.normpath(path.replace("\\", "/"))
        if norm in seen or norm in pr_filenames:
            return
        if not is_safe_repo_relative_path(norm):
            return
        seen.add(norm)
        paths.append(norm)

    for path in get_manual_extra_context_paths():
        add(path)

    if auto_php_include_context_enabled():
        for path in sorted(extract_php_include_paths_from_diff_files(diff_files)):
            add(path)

    return paths[: get_max_extra_context_files()]


def load_extra_context_files(
    diff_files: List[FilePatchInfo],
    fetch_file: Callable[[str], Optional[str]],
) -> Dict[str, str]:
    """
    Fetch extra context file contents.

    ``fetch_file`` should return decoded file text or None/empty when missing (404).
    """
    if not extra_context_files_enabled():
        return {}

    loaded: Dict[str, str] = {}
    for path in collect_extra_context_paths(diff_files):
        try:
            content = fetch_file(path)
        except Exception as e:
            get_logger().debug(
                "extra_context_files: skipped path",
                filename=path,
                artifact={"error": e},
            )
            continue
        if not content:
            get_logger().debug("extra_context_files: file not found or empty", filename=path)
            continue
        loaded[path] = _decode_legacy_bytes(content)
        get_logger().info("extra_context_files: loaded context file", filename=path)

    if loaded:
        get_logger().info(
            "extra_context_files: attached context files",
            extra={"files": list(loaded.keys())},
        )
    return loaded


def format_extra_context_section(filename: str, content: str) -> str:
    text = _decode_legacy_bytes(content)
    if not text:
        return ""
    return (
        f"\n\n{_EXTRA_CONTEXT_HEADER.format(filename=filename.strip())}\n"
        f"{_EXTRA_CONTEXT_NOTE}\n\n"
        f"{text.rstrip()}\n"
    )


def format_extra_context_sections(context_files: Dict[str, str]) -> str:
    if not context_files:
        return ""
    parts = [format_extra_context_section(path, content) for path, content in context_files.items()]
    return "".join(part for part in parts if part)
