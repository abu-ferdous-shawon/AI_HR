"""Load and validate the active YAML knowledge packs."""
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.config import settings
from app.core.sites import PACK_RE, SiteConfig, current_site

BUILTIN_PACKS = Path(__file__).resolve().parent

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
FieldName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")]


class KnowledgePackError(ValueError):
    """An enabled pack is missing or has invalid configuration."""


class PackYamlLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, (str, int)) or key in keys:
                raise yaml.YAMLError("Pack mappings require unique keys")
            keys.add(key)
        return super().construct_mapping(node, deep=deep)


class PackSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Overview(PackSection):
    description: Text = "Business knowledge for this site."
    flows: tuple[Text, ...] = ()
    supported_topics: tuple[Text, ...] = ()


class DocTypeKnowledge(PackSection):
    status_field: FieldName | None = None
    states: tuple[Text, ...] | dict[str | int, Text] = ()
    date_fields: tuple[FieldName, ...] = ()
    owner_link: FieldName | None = None


class NeverSend(PackSection):
    global_fields: tuple[FieldName, ...] = ()
    doctype_fields: dict[Text, tuple[FieldName, ...]] = Field(default_factory=dict)


class VocabularyEntry(PackSection):
    target_doctype: Text
    description: Text | None = None


class RecommendedReport(PackSection):
    name: Text
    when_to_use: Text


class Example(PackSection):
    question: Text
    intent: Text
    target_doctype: Text | None = None
    notes: Text | None = None


class KnowledgePack(PackSection):
    pack_name: Text
    version: Text = "1.0"
    modules: tuple[Text, ...] = ()
    overview: Overview = Field(default_factory=Overview)
    doctypes: dict[Text, DocTypeKnowledge] = Field(default_factory=dict)
    never_send: NeverSend = Field(default_factory=NeverSend)
    vocabulary: dict[Text, Text | VocabularyEntry] = Field(default_factory=dict)
    aliases: dict[Text, Text | tuple[Text, ...]] = Field(default_factory=dict)
    kpis: dict[Text, Text | VocabularyEntry] = Field(default_factory=dict)
    recommended_reports: tuple[RecommendedReport, ...] = ()
    master_hints: tuple[Text, ...] = ()
    examples: tuple[Example, ...] = ()


def load_packs(names: list[str] | tuple[str, ...], directory: str = "") -> tuple[KnowledgePack, ...]:
    """Read enabled packs in configuration order, preferring a site override by name."""
    packs = []
    for name in dict.fromkeys(names):
        if not PACK_RE.fullmatch(name):
            raise KnowledgePackError(f"Invalid knowledge pack name {name!r}")
        path = Path(directory).expanduser() / f"{name}.yaml" if directory else None
        if path is None or not path.exists():
            path = BUILTIN_PACKS / f"{name}.yaml"
        try:
            raw = yaml.load(path.read_text(encoding="utf-8"), Loader=PackYamlLoader)
        except (OSError, UnicodeError, yaml.YAMLError):
            raise KnowledgePackError(f"Unable to read knowledge pack {path}") from None
        try:
            pack = KnowledgePack.model_validate(raw)
        except ValidationError as exc:
            locations = ", ".join(".".join(map(str, error["loc"])) or "pack"
                                  for error in exc.errors(include_input=False))
            raise KnowledgePackError(f"Invalid knowledge pack {path} at {locations}") from None
        if pack.pack_name != name:
            raise KnowledgePackError(f"Expected pack_name {name!r} in {path}")
        packs.append(pack)
    return tuple(packs)


@lru_cache
def _configured_packs(site_name: str, names: tuple[str, ...], directory: str) -> tuple[KnowledgePack, ...]:
    return load_packs(names, directory)


def active_packs(site: SiteConfig | None = None) -> tuple[KnowledgePack, ...]:
    site = site or current_site()
    return _configured_packs(site.name, tuple(site.knowledge_packs), settings.knowledge_pack_dir)
