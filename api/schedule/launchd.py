"""launchd schedule helpers.

Marker module placed at the new path the plan calls for. Plist files live
in deploy/ and there is no current Python helper to wrap; this module
serves as the import target for downstream tooling.
"""
from pathlib import Path

LAUNCHD_PLIST_DIR = Path(__file__).resolve().parents[2] / "deploy"


def list_plists() -> list[Path]:
    return sorted(LAUNCHD_PLIST_DIR.glob("*.plist"))


__all__ = ["LAUNCHD_PLIST_DIR", "list_plists"]
