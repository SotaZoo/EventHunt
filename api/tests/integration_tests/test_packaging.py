import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_python_package_uses_api_entrypoint():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    scripts = data["project"]["scripts"]
    assert scripts["nyc-ai-scout"].split()[0] == "api.commands.main:app"
    assert scripts["nyc-ai"].split()[0] == "api.commands.main:app"


def test_python_package_includes_runtime_artifacts():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    force_include = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    assert force_include["db/migrate"] == "api/_runtime/db/migrate"
    assert force_include["deploy"] == "api/_runtime/deploy"


def test_production_docker_stage_copies_frontend_entry_directories():
    build_script = (ROOT / "web/build-discovery.mjs").read_text()
    entry_points = re.search(r"entryPoints:\s*\{([^}]+)\}", build_script)
    assert entry_points is not None
    entries = re.findall(r"\w+:\s*'([^']+)'", entry_points.group(1))
    assert entries
    docker_stage = (ROOT / "Dockerfile").read_text().split("RUN npm run build:discovery", 1)[0]
    for entry in entries:
        directory = Path(entry).parent.as_posix()
        assert f"COPY web/{directory} ./{directory}" in docker_stage
