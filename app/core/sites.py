"""One Frappe deployment = one site. Everything that differs per site lives here."""
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import yaml
from dotenv import dotenv_values
from pydantic import (
    BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator,
)

from app.config import settings
from app.core.secret_store import SecretsError, resolve_secret

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
HOST_RE = re.compile(r"^[a-z0-9.-]+$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PACK_RE = re.compile(r"^[a-z0-9_]+$")
SCHEDULE_RE = re.compile(r"^(manual|every:[1-9][0-9]{0,3})$")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class SiteConfigError(Exception):
    """The sites file is missing, unreadable or invalid. The message says what to fix."""


class SiteConfig(BaseModel):
    # extra="forbid": a typo in the sites file is an error, not a silent no-op.
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    hosts: list[str] = Field(default_factory=list)
    frappe_url: str
    api_key: SecretStr            # SecretStr prints as ********, so logs cannot leak it
    api_secret: SecretStr
    llm_provider: str
    llm_model: str
    gemini_api_key: SecretStr = Field(default=SecretStr(""), exclude=True)
    groq_api_key: SecretStr = Field(default=SecretStr(""), exclude=True)
    groq_fallback_model: str = ""
    embedding_provider: str
    embedding_model: str
    knowledge_packs: list[str] = Field(default_factory=list)
    indexing_schedule: str = "manual"
    index_include_doctypes: list[str] = Field(default_factory=list)
    index_exclude_doctypes: list[str] = Field(default_factory=list)
    write_doctypes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fallback_credentials(self):
        if self.groq_fallback_model.strip() and not self.groq_api_key.get_secret_value().strip():
            raise ValueError("Configure GROQ_API_KEY or the site's groq_api_key_env to enable GroqCloud")
        return self

    @field_validator("write_doctypes")
    @classmethod
    def _write_doctypes(cls, names):
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{0,139}", name.strip()) for name in names):
            raise ValueError("write_doctypes must contain valid DocType names")
        return list(dict.fromkeys(name.strip() for name in names))

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if not NAME_RE.match(v):
            raise ValueError("must be lower-case letters, digits, - or _ (max 41 characters)")
        return v

    @field_validator("hosts")
    @classmethod
    def _hosts(cls, v):
        clean = [h.strip().lower() for h in v]
        bad = [h for h in clean if not HOST_RE.match(h)]
        if bad:
            raise ValueError(f"hosts must be bare host names, no scheme, port or path: {bad}")
        return clean

    @field_validator("frappe_url")
    @classmethod
    def _url(cls, v):
        parsed = urlparse(v.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("must start with http:// or https://")
        return v.strip().rstrip("/")

    @field_validator("llm_provider")
    @classmethod
    def _provider(cls, value):
        name = value.strip().lower()
        if name != "gemini":
            raise ValueError("supported LLM provider: gemini")
        return name

    @field_validator("llm_model", "embedding_provider", "embedding_model")
    @classmethod
    def _nonempty(cls, value):
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @field_validator("knowledge_packs")
    @classmethod
    def _packs(cls, v):
        bad = [p for p in v if not PACK_RE.match(p)]
        if bad:
            raise ValueError(f"pack names must be lower_snake_case: {bad}")
        return v

    @field_validator("indexing_schedule")
    @classmethod
    def _schedule(cls, v):
        if not SCHEDULE_RE.match(v):
            raise ValueError("must be 'manual' or 'every:<hours>', for example every:24")
        return v


def _global_defaults() -> dict:
    return {
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model or settings.gemini_model,
        "gemini_api_key": settings.gemini_api_key,
        "groq_api_key": settings.groq_api_key,
        "groq_fallback_model": settings.groq_fallback_model,
        "embedding_provider": settings.embedding_provider,
        "embedding_model": settings.embedding_model,
        "knowledge_packs": [p.strip() for p in settings.knowledge_packs.split(",") if p.strip()],
        "indexing_schedule": settings.indexing_schedule,
        "index_include_doctypes": [d.strip() for d in settings.index_include_doctypes.split(",") if d.strip()],
        "index_exclude_doctypes": [d.strip() for d in settings.index_exclude_doctypes.split(",") if d.strip()],
        "write_doctypes": [d.strip() for d in settings.write_doctypes.split(",") if d.strip()],
    }


def _from_env() -> SiteConfig:
    """No sites file: the single site described by the FRAPPE_* settings."""
    return SiteConfig(name="default", frappe_url=settings.frappe_url,
                      api_key=settings.frappe_api_key, api_secret=settings.frappe_api_secret,
                      **_global_defaults())


def _secret(site: str, raw: dict, key: str, env_file: dict | None = None) -> SecretStr:
    var = raw.pop(f"{key}_env", None)
    if not isinstance(var, str) or not ENV_RE.fullmatch(var):
        raise SiteConfigError(f"site '{site}': '{key}_env' must name an environment variable")
    value = (os.environ.get(var, (env_file or {}).get(var)) or "").strip()
    if not value:
        raise SiteConfigError(f"site '{site}': environment variable {var} is not set or is empty")
    try:
        return SecretStr(resolve_secret(value, str(var)))
    except SecretsError as exc:
        raise SiteConfigError(f"site '{site}': {var}: {exc}") from exc


def _from_file(path: Path) -> tuple[dict[str, SiteConfig], str]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SiteConfigError(f"Could not read {path}: {exc.strerror}") from None
    except yaml.YAMLError:
        raise SiteConfigError(f"Invalid YAML in {path}") from None
    if not isinstance(data, dict) or not isinstance(data.get("sites"), dict) or not data["sites"]:
        raise SiteConfigError(f"{path.name} must contain a non-empty 'sites:' mapping")

    unknown_top = set(data) - {"default", "sites"}
    if unknown_top:
        raise SiteConfigError(f"{path.name}: unknown top-level keys: {sorted(unknown_top)}")

    env_file = dotenv_values(settings.model_config["env_file"])
    sites: dict[str, SiteConfig] = {}
    for name, raw in data["sites"].items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise SiteConfigError("Each site must have a string name and a settings mapping")
        raw = dict(raw)
        secrets = {key: _secret(name, raw, key, env_file) for key in ("api_key", "api_secret")}
        if {"api_key", "api_secret", "gemini_api_key", "groq_api_key"} & raw.keys():
            raise SiteConfigError(f"site '{name}': use environment variable references for secrets")
        for key in ("gemini_api_key", "groq_api_key"):
            if f"{key}_env" in raw:
                secrets[key] = _secret(name, raw, key, env_file)
        try:
            sites[name] = SiteConfig(name=str(name), **{**_global_defaults(), **raw, **secrets})
        except ValidationError as exc:
            lines = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            raise SiteConfigError(f"site '{name}': {lines}") from None

    seen: dict[str, str] = {}
    for site in sites.values():
        for host in site.hosts:
            if host in seen:
                raise SiteConfigError(f"host '{host}' belongs to both '{seen[host]}' and '{site.name}'")
            seen[host] = site.name

    default = str(data.get("default") or next(iter(sites)))
    if default not in sites:
        raise SiteConfigError(f"default site '{default}' is not listed under sites")
    return sites, default


@lru_cache
def _loaded() -> tuple[dict[str, SiteConfig], str]:
    if not settings.sites_file.strip():
        site = _from_env()
        return {site.name: site}, site.name
    path = Path(settings.sites_file).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return _from_file(path)


def clear_site_cache() -> None:
    _loaded.cache_clear()


def get_sites() -> dict[str, SiteConfig]:
    return _loaded()[0]


def get_default_site() -> SiteConfig:
    sites, default = _loaded()
    return sites[default]


def site_for_host(host: str | None) -> SiteConfig:
    """The site whose 'hosts' list names this Host header, else the default site."""
    name = (host or "").strip().lower()
    if not name.startswith("["):                 # not an IPv6 literal: drop the :port
        name = name.split(":")[0]
    for site in get_sites().values():
        if name in site.hosts:
            return site
    return get_default_site()


# ---------- the site of the request being handled ----------

_CURRENT: ContextVar[str | None] = ContextVar("current_site", default=None)


def current_site() -> SiteConfig:
    name = _CURRENT.get()
    return get_sites()[name] if name else get_default_site()


@contextmanager
def use_site(name: str):
    """Run a block as this site. Raises KeyError for a site that is not configured."""
    get_sites()[name]
    token = _CURRENT.set(name)
    try:
        yield
    finally:
        _CURRENT.reset(token)
