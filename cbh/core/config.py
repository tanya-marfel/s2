"""Configuration and secret loading.

Secrets come from the environment, or from a gitignored .env file. They are
never hardcoded, never logged, and never written to generated artifacts.
"""

import os
from typing import Dict, Optional

_ENV_LOADED = False


def load_env(path: str = ".env") -> None:
    """Load KEY=VALUE pairs from .env into os.environ without overriding real env vars."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True

    if not os.path.exists(path):
        return

    parsed: Dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key:
                continue
            # A blank placeholder must not shadow a real value defined later in
            # the file, which happens when .env is seeded from .env.example.
            if value or key not in parsed:
                parsed[key] = value

    for key, value in parsed.items():
        # Real environment variables win, so shell exports take precedence.
        if value and key not in os.environ:
            os.environ[key] = value


class MissingSecret(RuntimeError):
    pass


def get_secret(name: str, required: bool = True) -> Optional[str]:
    """Return a secret, or raise with guidance. The value is never logged."""
    load_env()
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if required:
        raise MissingSecret(
            "%s is not set.\n"
            "  cp .env.example .env && chmod 600 .env\n"
            "  then add your key, or export %s=... in your shell" % (name, name)
        )
    return None


def get_flag(name: str, default: bool = False) -> bool:
    load_env()
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def get_setting(name: str, default: str = "") -> str:
    load_env()
    return os.environ.get(name, "").strip() or default


def mask(secret: Optional[str]) -> str:
    """Render a secret safely for diagnostics."""
    if not secret:
        return "<unset>"
    if len(secret) <= 8:
        return "*" * len(secret)
    return "%s...%s (%d chars)" % (secret[:4], secret[-2:], len(secret))


def status() -> Dict[str, str]:
    """Masked view of configured credentials, safe to print."""
    load_env()
    return {
        "GEMINI_API_KEY": mask(get_secret("GEMINI_API_KEY", required=False)),
        "CONDENSE_API_KEY": mask(get_secret("CONDENSE_API_KEY", required=False)),
        "USE_CONDENSE": str(get_flag("USE_CONDENSE", True)),
        "CONDENSE_UPSTREAM_URL": get_setting("CONDENSE_UPSTREAM_URL", "<unset>"),
        "GEMINI_MODEL": get_setting("GEMINI_MODEL", "gemini-2.5-pro"),
    }


if __name__ == "__main__":
    for k, v in status().items():
        print("%-22s %s" % (k, v))
