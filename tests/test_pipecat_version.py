"""All supported entry points must use the same exact Pipecat release."""
import importlib.metadata
from pathlib import Path
import tomllib

import yaml

ROOT = Path(__file__).resolve().parents[1]
PIN = "pipecat-ai[elevenlabs,silero,websocket]==1.7.0"


def test_pipecat_version_is_exact_everywhere():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert PIN in project["project"]["dependencies"]
    assert PIN in manifest["python_dependencies"]
    assert importlib.metadata.version("pipecat-ai") == "1.7.0"


def test_install_hint_matches_the_pin():
    source = (ROOT / "__init__.py").read_text()
    assert f"'{PIN}'" in source
