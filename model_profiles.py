"""Read model configuration without importing the task engine or SDK."""

import os

from dotenv import load_dotenv


def environment_model_profiles() -> dict:
    """Read the default and suffixed MODEL_NAME/API_KEY/BASE_URL profiles."""
    load_dotenv()
    profiles = {}
    for raw_key, value in os.environ.items():
        key = raw_key.upper()
        if not value:
            continue
        if key == "MODEL_NAME":
            profiles["default"] = {
                "model": value,
                "api_key": os.environ.get("API_KEY"),
                "base_url": os.environ.get("BASE_URL") or None,
            }
        elif key.startswith("MODEL_NAME_"):
            name = key[len("MODEL_NAME_"):].lower()
            profiles[name] = {
                "model": value,
                "api_key": os.environ.get(f"API_KEY_{name.upper()}") or os.environ.get("API_KEY"),
                "base_url": os.environ.get(f"BASE_URL_{name.upper()}") or None,
            }
    if "default" not in profiles and os.environ.get("API_KEY"):
        profiles["default"] = {
            "model": os.environ.get("MODEL_NAME", "gpt-4o-mini"),
            "api_key": os.environ.get("API_KEY"),
            "base_url": os.environ.get("BASE_URL") or None,
        }
    for name, profile in profiles.items():
        suffix = "" if name == "default" else f"_{name.upper()}"
        api_type = os.environ.get(f"API_TYPE{suffix}")
        if api_type:
            profile["api_type"] = api_type.strip().lower()
    return profiles


def list_model_profiles(root=None) -> dict:
    """Apply workbench overrides while preserving the original .env profiles."""
    from workbench_settings import ROOT, WorkbenchSettings
    return WorkbenchSettings(ROOT if root is None else root).profiles(environment_model_profiles())
