"""Settings values with environment-variable fallbacks. Env values are not written back."""

from __future__ import annotations

import os

from ..database import get_setting


def setting_or_env(setting_key: str, env_name: str) -> tuple[str, str]:
    stored = (get_setting(setting_key) or "").strip()
    if stored:
        return stored, "settings"
    env = (os.environ.get(env_name) or "").strip()
    if env:
        return env, "env"
    return "", ""
