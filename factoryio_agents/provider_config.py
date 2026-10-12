"""Restricted two-level scalar YAML configuration; keys stay in process memory."""
from __future__ import annotations
import json
from pathlib import Path
import re
import urllib.parse

from .contracts import require


def load_providers(path: Path) -> dict:
    result, section = {}, None
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"( *)([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*?)\s*", line)
        require(match is not None, f"unsupported provider YAML at line {number}")
        indent, key, value = match.groups()
        if not indent:
            require(not value and key not in result, f"invalid provider section at line {number}")
            section = key; result[key] = {}
        else:
            require(section is not None and key.lower() not in result[section], f"duplicate provider field at line {number}")
            if value.startswith('"'):
                try:
                    value = json.loads(value)
                except ValueError:
                    raise ValueError(f"invalid provider scalar at line {number}") from None
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1].replace("''", "'")
            else:
                value = value.split(" #", 1)[0].strip()
            require(isinstance(value, str) and bool(value), f"empty provider scalar at line {number}")
            result[section][key.lower()] = value
    for name, provider in result.items():
        require(all(provider.get(key) for key in ("base_url", "model_name", "api_key")), f"provider fields missing: {name}")
        url = urllib.parse.urlsplit(provider["base_url"])
        require(url.scheme == "https" and bool(url.hostname) and not any((url.username, url.password, url.query, url.fragment)),
                f"provider requires credential-free HTTPS endpoint: {name}")
    require(bool(result), "provider config empty")
    return result
