"""Tiny dependency-free loader for secrets.env, since nothing else was
actually reading that file into the environment -- it was just sitting
there. Real environment variables always win over the file (same
convention as most dotenv tools), so this is safe to call unconditionally.
"""

from __future__ import annotations

import os


def load_secrets_env(path: str = "secrets.env") -> None:
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value
