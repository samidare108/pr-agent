"""
Rebuild PR prompt diffs from correctly decoded file contents.

For legacy Japanese encodings (EUC-JP, CP932), GitHub API patches and intermediate
patch processing may contain mojibake. This module bypasses corrupted intermediate
strings by rebuilding unified diffs from FilePatchInfo.base_file / head_file
(fetched and decoded in the git provider) immediately before prompt rendering.
"""

from __future__ import annotations

import difflib
import re
from typing import Dict, List, Union

from pr_agent.algo.git_patch_processing import (
    decouple_and_convert_to_hunks_with_lines_numbers,
    extend_patch,
)
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.log import get_logger

_FILE_SECTION_RE = re.compile(
    r"(## File:\s*'(?P<filename>[^']+)')",
    re.MULTILINE,
)


def legacy_encoding_repair_enabled() -> bool:
    return bool(get_settings().config.get("legacy_encoding_repair", False))


def get_legacy_file_encodings() -> List[str]:
    encodings = get_settings().config.get("legacy_file_encodings", ["utf-8", "euc_jp", "cp932"])
    if isinstance(encodings, str):
        encodings = [enc.strip() for enc in encodings.split(",") if enc.strip()]
    return encodings or ["utf-8", "euc_jp", "cp932"]


def decode_legacy_bytes(content: Union[str, bytes, bytearray, None]) -> str:
    """Decode raw file bytes using configured legacy encodings; pass through str."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (bytes, bytearray)):
        for encoding in get_legacy_file_encodings():
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="replace")
    return str(content)


def _normalize_lines(text: str) -> List[str]:
    if not text:
        return []
    normalized = text if text.endswith("\n") else text + "\n"
    return normalized.splitlines(keepends=True)


def rebuild_unified_patch(file: FilePatchInfo) -> str:
    """
    Build a unified diff from decoded base/head file contents.

    Handles added files (empty base), deleted files (empty head), and renames
    using the post-rename filename on head.
    """
    base = decode_legacy_bytes(file.base_file)
    head = decode_legacy_bytes(file.head_file)

    if file.edit_type == EDIT_TYPE.ADDED:
        base = ""
    elif file.edit_type == EDIT_TYPE.DELETED:
        head = ""

    if not base and not head:
        return file.patch or ""

    diff_lines = difflib.unified_diff(
        _normalize_lines(base),
        _normalize_lines(head),
        fromfile=f"a/{file.filename}",
        tofile=f"b/{file.filename}",
    )
    return "".join(diff_lines)


def _can_rebuild_from_content(file: FilePatchInfo) -> bool:
    if file.edit_type == EDIT_TYPE.ADDED:
        return bool(decode_legacy_bytes(file.head_file))
    if file.edit_type == EDIT_TYPE.DELETED:
        return bool(decode_legacy_bytes(file.base_file))
    return bool(decode_legacy_bytes(file.head_file)) or bool(decode_legacy_bytes(file.base_file))


def _format_file_section(
    file: FilePatchInfo,
    *,
    add_line_numbers_to_hunks: bool,
    patch_extra_lines_before: int,
    patch_extra_lines_after: int,
    fallback_section: str = "",
) -> str:
    if file.edit_type == EDIT_TYPE.DELETED and not decode_legacy_bytes(file.base_file):
        return fallback_section or f"\n\n## File: '{file.filename.strip()}' was deleted\n"

    if not _can_rebuild_from_content(file):
        if fallback_section:
            get_logger().debug(
                "legacy_encoding_diff: keeping original section (no full file content)",
                filename=file.filename,
            )
            return fallback_section
        patch = file.patch or ""
        if not patch:
            return ""
        if add_line_numbers_to_hunks:
            return decouple_and_convert_to_hunks_with_lines_numbers(patch, file)
        return f"\n\n## File: '{file.filename.strip()}'\n\n{patch.strip()}\n"

    patch = rebuild_unified_patch(file)
    if not patch:
        return fallback_section

    if patch_extra_lines_before or patch_extra_lines_after:
        try:
            patch = extend_patch(
                decode_legacy_bytes(file.base_file),
                patch,
                patch_extra_lines_before,
                patch_extra_lines_after,
                file.filename,
                new_file_str=decode_legacy_bytes(file.head_file),
            )
        except Exception as e:
            get_logger().warning(
                "legacy_encoding_diff: extend_patch failed, using rebuilt patch without extra context",
                filename=file.filename,
                artifact={"error": e},
            )

    if add_line_numbers_to_hunks:
        return decouple_and_convert_to_hunks_with_lines_numbers(patch, file)
    return f"\n\n## File: '{file.filename.strip()}'\n\n{patch.strip()}\n"


def _extract_file_sections(patches_diff: str) -> Dict[str, str]:
    """Split a multi-file prompt diff into per-file sections keyed by filename."""
    if not patches_diff:
        return {}

    matches = list(_FILE_SECTION_RE.finditer(patches_diff))
    if not matches:
        return {}

    sections: Dict[str, str] = {}
    for index, match in enumerate(matches):
        filename = match.group("filename").strip()
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(patches_diff)
        sections[filename] = patches_diff[start:end]
    return sections


def _ordered_filenames_from_diff(patches_diff: str) -> List[str]:
    return [m.group("filename").strip() for m in _FILE_SECTION_RE.finditer(patches_diff)]


def build_repaired_file_sections(
    git_provider: GitProvider,
    patches_diff: str,
    *,
    add_line_numbers_to_hunks: bool = True,
    patch_extra_lines_before: int = 0,
    patch_extra_lines_after: int = 0,
) -> Dict[str, str]:
    original_sections = _extract_file_sections(patches_diff)
    try:
        diff_files = git_provider.get_diff_files()
    except Exception as e:
        get_logger().warning(
            "legacy_encoding_diff: failed to load diff files, skipping repair",
            artifact={"error": e},
        )
        return original_sections

    repaired: Dict[str, str] = {}
    for file in diff_files:
        filename = file.filename.strip()
        repaired[filename] = _format_file_section(
            file,
            add_line_numbers_to_hunks=add_line_numbers_to_hunks,
            patch_extra_lines_before=patch_extra_lines_before,
            patch_extra_lines_after=patch_extra_lines_after,
            fallback_section=original_sections.get(filename, ""),
        )
    return repaired


def repair_prompt_diff(
    git_provider: GitProvider,
    patches_diff: str,
    *,
    add_line_numbers_to_hunks: bool = True,
    patch_extra_lines_before: int | None = None,
    patch_extra_lines_after: int | None = None,
) -> str:
    """
    Replace prompt diff content with UTF-8 text rebuilt from decoded file contents.

    When legacy_encoding_repair is disabled, returns patches_diff unchanged.
    Preserves per-chunk file ordering when patches_diff covers a subset of files.
    """
    if not legacy_encoding_repair_enabled() or not patches_diff:
        return patches_diff

    if patch_extra_lines_before is None:
        patch_extra_lines_before = int(get_settings().config.get("patch_extra_lines_before", 0) or 0)
    if patch_extra_lines_after is None:
        patch_extra_lines_after = int(get_settings().config.get("patch_extra_lines_after", 0) or 0)

    sections = build_repaired_file_sections(
        git_provider,
        patches_diff,
        add_line_numbers_to_hunks=add_line_numbers_to_hunks,
        patch_extra_lines_before=patch_extra_lines_before,
        patch_extra_lines_after=patch_extra_lines_after,
    )
    if not sections:
        return patches_diff

    filenames = _ordered_filenames_from_diff(patches_diff) or list(sections.keys())
    repaired_parts = [sections[name] for name in filenames if sections.get(name)]
    if not repaired_parts:
        return patches_diff

    get_logger().info(
        "legacy_encoding_diff: repaired prompt diff from decoded file contents",
        extra={"files": filenames},
    )
    return "".join(repaired_parts)
