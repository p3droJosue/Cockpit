"""
config_loader.py
----------------
Loads config.yaml (the template, safe to commit) and deep-merges
config.local.yaml on top of it if present (gitignored, holds secrets
and per-machine overrides).
"""

from pathlib import Path

import yaml


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base; override wins on conflicts."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str = "config.yaml",
                local_path: str = "config.local.yaml") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    local = Path(local_path)
    if local.exists():
        with open(local, "r", encoding="utf-8") as f:
            overrides = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, overrides)

    return cfg
