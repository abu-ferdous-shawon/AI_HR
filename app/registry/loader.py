import copy
import json
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.config import settings
from app.registry.models import Profile

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
SUFFIXES = (".yaml", ".yml", ".json")
MAX_EXTENDS_DEPTH = 3


class RegistryError(Exception):
    """A profile is missing, unreadable or invalid. The message says what to fix."""


def _resolve_path(ref: str, relative_to: Path | None = None) -> Path:
    ref = str(ref).strip()
    if not ref:
        raise RegistryError("A profile name or path is empty.")
    if "/" in ref or "\\" in ref or Path(ref).suffix.lower() in SUFFIXES:
        path = Path(ref).expanduser()
        if not path.is_absolute():
            path = (relative_to or PROJECT_ROOT) / path
        candidates = [path]
    else:                                   # a bare name: look in the shipped profiles
        candidates = [PROFILE_DIR / f"{ref}{suffix}" for suffix in SUFFIXES]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RegistryError(f"Profile '{ref}' was not found. Looked for: "
                        + ", ".join(str(c) for c in candidates))


def _read(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise RegistryError(f"Could not read {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"{path.name} must contain a mapping at the top level.")
    return data


def _merge(base: dict, overlay: dict) -> dict:
    """Mappings merge key by key; lists and values in the overlay replace the base."""
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _load_raw(ref: str, relative_to: Path | None = None, chain: tuple = ()) -> dict:
    path = _resolve_path(ref, relative_to)
    if path in chain:
        raise RegistryError("Circular 'extends': "
                            + " -> ".join(p.name for p in (*chain, path)))
    if len(chain) >= MAX_EXTENDS_DEPTH:
        raise RegistryError("'extends' is nested too deeply.")
    data = _read(path)
    parent = data.pop("extends", None)
    if parent:
        base = _load_raw(parent, path.parent, (*chain, path))
        data = _merge(base, data)
    return data


def _format(exc: ValidationError) -> str:
    errors = exc.errors()
    lines = [f"  {'.'.join(str(p) for p in err['loc']) or 'profile'}: {err['msg']}"
             for err in errors[:20]]
    if len(errors) > 20:
        lines.append(f"  ... and {len(errors) - 20} more")
    return "\n".join(lines)


def build_profile(raw: dict) -> Profile:
    try:
        return Profile.model_validate(raw)
    except ValidationError as exc:
        raise RegistryError(_format(exc)) from exc


def load_profile(ref: str) -> Profile:
    """Load a profile by shipped name or file path, following 'extends'."""
    return build_profile(_load_raw(ref))


@lru_cache
def get_profile() -> Profile:
    """The profile chosen by REGISTRY_PROFILE, loaded once."""
    return load_profile(settings.registry_profile)


def clear_profile_cache() -> None:
    get_profile.cache_clear()