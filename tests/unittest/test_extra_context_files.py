from pr_agent.algo.extra_context_files import (
    collect_extra_context_paths,
    extract_php_include_paths_from_diff_files,
    format_extra_context_sections,
    load_extra_context_files,
    resolve_include_path_candidates,
)
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings


def test_extract_php_include_from_added_line():
    patch = (
        "@@ -1,3 +1,4 @@\n"
        " line\n"
        "+require_once 'src/Config/SystemConst.php';\n"
        " line2\n"
    )
    file = FilePatchInfo("", "", patch, "app/bootstrap.php", edit_type=EDIT_TYPE.MODIFIED)
    paths = extract_php_include_paths_from_diff_files([file])
    assert "src/Config/SystemConst.php" in paths


def test_resolve_relative_include_path():
    candidates = resolve_include_path_candidates("app/pages/index.php", "../Config/SystemConst.php")
    assert "app/Config/SystemConst.php" in candidates


def test_collect_skips_pr_files_and_merges_manual():
    get_settings().set("config.enable_extra_context_files", True)
    get_settings().set("config.auto_php_include_context", True)
    get_settings().set("config.extra_context_files", ["src/Manual.php"])
    diff_files = [
        FilePatchInfo("", "", "+require 'src/Config/SystemConst.php';\n", "main.php", edit_type=EDIT_TYPE.MODIFIED),
        FilePatchInfo("", "content", "", "src/Manual.php", edit_type=EDIT_TYPE.MODIFIED),
    ]
    paths = collect_extra_context_paths(diff_files)
    assert "src/Config/SystemConst.php" in paths
    assert "src/Manual.php" not in paths
    assert "main.php" not in paths


def test_load_extra_context_files_fetches_and_decodes():
    get_settings().set("config.enable_extra_context_files", True)
    get_settings().set("config.auto_php_include_context", False)
    get_settings().set("config.extra_context_files", ["src/Config/SystemConst.php"])
    raw = "定数定義".encode("euc_jp")

    def fetch(path):
        assert path == "src/Config/SystemConst.php"
        return raw.decode("latin-1")  # simulate mis-decoded transport; repair uses bytes path below

    # pass bytes through fetch by returning bytes - load uses _decode_legacy_bytes
    def fetch_bytes(path):
        return raw

    loaded = load_extra_context_files([], fetch_bytes)
    assert "src/Config/SystemConst.php" in loaded
    assert "定数定義" in loaded["src/Config/SystemConst.php"]


def test_format_extra_context_sections():
    sections = format_extra_context_sections({"src/Config/SystemConst.php": "define('X', 1);\n"})
    assert "## Related context file: 'src/Config/SystemConst.php'" in sections
    assert "not part of the PR diff" in sections
    assert "define('X', 1);" in sections
