from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_approved_top_level_directories_exist():
    expected = ["api", "web", "e2e", "db", "deploy", "doc", ".ai", ".github"]
    missing = [path for path in expected if not (ROOT / path).exists()]
    assert missing == []


def test_rejected_top_level_directories_are_not_target_surfaces():
    rejected = [
        ROOT / "doc" / "plans",
        ROOT / "doc" / "checkpoints",
        ROOT / "doc" / "checklists",
        ROOT / "doc" / "architecture",
        ROOT / "doc" / "development" / "design_documents",
    ]
    assert [str(path.relative_to(ROOT)) for path in rejected if path.exists()] == []


def test_no_root_tests_directory_in_target_tree():
    assert not (ROOT / "tests").exists()
