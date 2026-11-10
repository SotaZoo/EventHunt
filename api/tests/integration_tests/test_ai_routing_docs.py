from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_ai_router_exists_and_names_required_modules():
    router = (ROOT / ".ai" / "AGENTS.md").read_text()
    for module in [
        "git.md",
        "merge-requests.md",
        "code-review.md",
        "database.md",
        "testing.md",
        "code-style.md",
        "ci-cd.md",
        "security.md",
        "runtime.md",
        "ai.md",
    ]:
        assert module in router


def test_tool_adapters_route_to_ai_router():
    for path in [
        ROOT / "AGENTS.md",
        ROOT / "CLAUDE.md",
        ROOT / ".claude" / "skills" / "nyc-ai-events-coding-principles" / "SKILL.md",
        ROOT / ".agents" / "skills" / "nyc-ai-events-coding-principles" / "SKILL.md",
    ]:
        assert ".ai/AGENTS.md" in path.read_text()


def test_required_directory_routers_exist():
    for relpath in [
        "api/AGENTS.md",
        "web/AGENTS.md",
        "e2e/AGENTS.md",
        "db/AGENTS.md",
        "deploy/AGENTS.md",
        "doc/AGENTS.md",
    ]:
        text = (ROOT / relpath).read_text()
        assert ".ai/AGENTS.md" in text
