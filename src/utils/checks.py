"""Checks that are performed to configuration options."""

import os
import importlib
import importlib.util
from types import ModuleType
from typing import Optional
from pydantic import FilePath


class InvalidConfigurationError(Exception):
    """Lightspeed configuration is invalid."""


def get_attribute_from_file(data: dict, file_name_key: str) -> Optional[str]:
    """Retrieve value of an attribute from a file."""
    file_path = data.get(file_name_key)
    if file_path is not None:
        with open(file_path, encoding="utf-8") as f:
            return f.read().rstrip()
    return None


def file_check(path: FilePath, desc: str) -> None:
    """Check that path is a readable regular file."""
    if not os.path.isfile(path):
        raise InvalidConfigurationError(f"{desc} '{path}' is not a file")
    if not os.access(path, os.R_OK):
        raise InvalidConfigurationError(f"{desc} '{path}' is not readable")


def import_python_module(profile_name: str, profile_path: str) -> ModuleType | None:
    """Import a Python module from a file path."""
    spec = importlib.util.spec_from_file_location(profile_name, profile_path)
    if spec and spec.loader:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return None


def is_valid_profile(profile_module: ModuleType) -> bool:
    """Validate that a profile module has the required PROFILE_CONFIG structure."""
    if not hasattr(profile_module, "PROFILE_CONFIG"):
        return False

    profile_config = getattr(profile_module, "PROFILE_CONFIG", {})
    if not isinstance(profile_config, dict):
        return False

    return "system_prompts" in profile_config
