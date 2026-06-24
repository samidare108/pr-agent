from pr_agent.algo.legacy_encoding_diff import (
    build_repaired_file_sections,
    decode_legacy_bytes,
    rebuild_unified_patch,
    repair_prompt_diff,
)
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings


def test_decode_legacy_bytes_euc_jp():
    raw = "日本語コメント".encode("euc_jp")
    assert decode_legacy_bytes(raw) == "日本語コメント"
    assert decode_legacy_bytes("already utf-8") == "already utf-8"


def test_rebuild_unified_patch_added_file():
    file = FilePatchInfo(
        base_file="",
        head_file="line1\n日本語\n",
        patch="",
        filename="new.sql",
        edit_type=EDIT_TYPE.ADDED,
    )
    patch = rebuild_unified_patch(file)
    assert "+line1" in patch
    assert "+日本語" in patch


def test_rebuild_unified_patch_modified_file():
    file = FilePatchInfo(
        base_file="old\n",
        head_file="new\n日本語\n",
        patch="",
        filename="prog.cbl",
        edit_type=EDIT_TYPE.MODIFIED,
    )
    patch = rebuild_unified_patch(file)
    assert "-old" in patch
    assert "+new" in patch
    assert "+日本語" in patch


def test_rebuild_unified_patch_deleted_file():
    file = FilePatchInfo(
        base_file="gone\n",
        head_file="",
        patch="",
        filename="old.sql",
        edit_type=EDIT_TYPE.DELETED,
    )
    patch = rebuild_unified_patch(file)
    assert "-gone" in patch


def test_repair_prompt_diff_replaces_mojibake_section():
    get_settings().set("config.legacy_encoding_repair", True)
    good_head = "SELECT 1 -- 日本語\n"
    file = FilePatchInfo(
        base_file="",
        head_file=good_head,
        patch="",
        filename="query.sql",
        edit_type=EDIT_TYPE.ADDED,
    )

    class FakeProvider:
        def get_diff_files(self):
            return [file]

    garbled = "\n\n## File: 'query.sql'\n\n@@ -0,0 +1 @@\n+SELECT 1 -- æ—¥æœ¬èªž\n"
    repaired = repair_prompt_diff(FakeProvider(), garbled, add_line_numbers_to_hunks=False)

    assert "日本語" in repaired
    assert "æ—¥æœ¬" not in repaired
    assert "## File: 'query.sql'" in repaired


def test_build_repaired_file_sections_preserves_chunk_order():
    files = [
        FilePatchInfo("", "a\n", "", "a.txt", edit_type=EDIT_TYPE.ADDED),
        FilePatchInfo("", "b\n", "", "b.txt", edit_type=EDIT_TYPE.ADDED),
    ]

    class FakeProvider:
        def get_diff_files(self):
            return files

    chunk = "\n\n## File: 'b.txt'\n\n+garbled\n"
    sections = build_repaired_file_sections(FakeProvider(), chunk, add_line_numbers_to_hunks=False)
    assert "b\n" in sections["b.txt"]
