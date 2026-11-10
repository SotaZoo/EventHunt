from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_required_docs_exist():
    required = [
        "doc/administration/authentication.md",
        "doc/administration/deployment.md",
        "doc/administration/database.md",
        "doc/administration/operations.md",
        "doc/administration/security.md",
        "doc/development/architecture.md",
        "doc/development/api.md",
        "doc/development/web.md",
        "doc/development/testing.md",
        "doc/development/database.md",
    ]
    missing = [path for path in required if not (ROOT / path).exists()]
    assert missing == []
