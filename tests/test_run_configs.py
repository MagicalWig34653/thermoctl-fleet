"""Shared PyCharm launch targets stay portable and executable."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_shared_run_configurations_have_valid_local_targets() -> None:
    files = sorted((REPO_ROOT / ".run").glob("*.run.xml"))
    assert len(files) == 18
    names: set[str] = set()
    for path in files:
        config = ET.parse(path).getroot().find("configuration")  # noqa: S314 - repo-owned XML
        assert config is not None
        assert config.get("folderName") in {
            "Fleet", "Tests", "Prüfungen", "Werkzeuge", "Mac-Test-VM"
        }
        name = config.get("name")
        assert name and name not in names
        names.add(name)
        options = {item.get("name"): item.get("value", "") for item in config.findall("option")}
        assert all(not re.search(r"/(?:Users|home)/[^/]+/", value) for value in options.values())
        # Only harmless env vars -- never anything that could be a secret.
        envs = {env.get("name"): env.get("value") for env in config.findall("envs/env")}
        assert envs in ({}, {"PYTHONUNBUFFERED": "1"})
        if config.get("type") in {"PythonConfigurationType", "tests"}:
            # Without the module element PyCharm cannot resolve "use the
            # project interpreter" (IS_MODULE_SDK) and refuses to start; without
            # content roots on PYTHONPATH `tools.*` modules are not importable.
            module_element = config.find("module")
            assert module_element is not None
            assert module_element.get("name") == "thermoctl-fleet"
            assert options["ADD_CONTENT_ROOTS"] == "true"
            assert options["ADD_SOURCE_ROOTS"] == "true"
        if config.get("type") == "PythonConfigurationType":
            assert options["IS_MODULE_SDK"] == "true"
            assert options["SDK_HOME"] == ""
            assert options["WORKING_DIRECTORY"] == "$PROJECT_DIR$"
            assert options["MODULE_MODE"] == "true"
            module = options["SCRIPT_NAME"]
            if module.startswith("tools."):
                assert (REPO_ROOT / (module.replace(".", "/") + ".py")).is_file()
            else:
                assert module in {"ruff", "mypy", "http.server"}
        elif config.get("type") == "tests":
            assert config.get("factoryName") == "py.test"
            assert options["_new_target"] == '"$PROJECT_DIR$/tests"'
            assert options["_new_targetType"] == '"PATH"'
            assert options["IS_MODULE_SDK"] == "true"
            assert options["SDK_HOME"] == ""
        else:
            assert config.get("type") == "ShConfigurationType"
            assert config.get("factoryName") == "Shell Script"
            assert options["EXECUTE_SCRIPT_FILE"] == ("true" if options["SCRIPT_PATH"] else "false")
            # PyCharm needs an explicit interpreter; the repo's scripts are bash.
            assert options["INTERPRETER_PATH"] == "/bin/bash"
            if options["SCRIPT_PATH"]:
                assert (REPO_ROOT / options["SCRIPT_PATH"].replace("$PROJECT_DIR$/", "")).is_file()
                assert options["SCRIPT_OPTIONS"] in {
                    "status", "create", "start", "enroll", "logs", "stop"
                }
            else:
                assert options["SCRIPT_TEXT"] == "go vet ./... && go test ./..."
                working = options["SCRIPT_WORKING_DIRECTORY"].replace("$PROJECT_DIR$/", "")
                assert (REPO_ROOT / working).is_dir()
