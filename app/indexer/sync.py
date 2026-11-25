"""Build and inspect incremental metadata snapshots through the indexing service purpose."""
import logging
import shutil
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone

from app.config import settings
from app.core.sites import SiteConfig, use_site
from app.frappe_client.client import FrappeError, get_service_client
from app.frappe_client.schema_extractor import MetadataError, read_catalog, read_report, read_schema
from app.frappe_client.service_purpose import service_purpose
from app.indexer.alias_resolver import build_maps, normalize
from app.indexer.cards import build_cards
from app.indexer.embeddings import create_embedder
from app.indexer.util import digest
from app.indexer.vector_store import IndexError, IndexStore, read_json, read_tier, search_tier, write_json, write_tier
from app.knowledge_packs.loader import load_packs
from app.knowledge_packs.redactor import NEVER_SEND_SCHEMA_VERSION, NeverSendPolicy

log = logging.getLogger("hr_ai.indexing")
SCHEMA_VERSION = 1


def _store(site: SiteConfig) -> IndexStore:
    return IndexStore(settings.metadata_index_dir, site.name)


def _source(site: SiteConfig) -> str:
    return digest(site.frappe_url)


def _indexing_account(site: SiteConfig) -> str:
    return digest(site.api_key.get_secret_value())


def _embedding_key(site: SiteConfig) -> str:
    provider = site.embedding_provider.casefold()
    model = "" if provider == "local" else site.embedding_model
    return digest([SCHEMA_VERSION, provider, model])


def _catalog(client, site: SiteConfig):
    return read_catalog(client, include=site.index_include_doctypes, exclude=site.index_exclude_doctypes)


def _pack_hash(packs) -> str:
    sections = {"pack_name", "aliases", "vocabulary", "kpis", "never_send"}
    return digest([NEVER_SEND_SCHEMA_VERSION, [pack.model_dump(mode="json", include=sections) for pack in packs]])


def _scan(site: SiteConfig, client, pack_hash: str):
    catalog = _catalog(client, site)
    fingerprint = digest([
        SCHEMA_VERSION, _source(site), _indexing_account(site), _embedding_key(site), pack_hash, asdict(catalog),
    ])
    return catalog, fingerprint


def _report(path, manifest: dict, state: str) -> dict:
    return {
        "state": state, "site": manifest["site"], "schema_hash": manifest["schema_hash"],
        "path": str(path), "synced_at": manifest["synced_at"], "counts": manifest["counts"],
        "unavailable": manifest["unavailable"],
    }


def _read_changes(versions: dict, previous: dict, kind: str, label: str, loader) -> tuple[dict, list[str]]:
    result, unavailable = {}, []
    total = len(versions)
    log.info("Checking %d %s", total, label)
    for position, (name, revision) in enumerate(sorted(versions.items()), start=1):
        if position > 1 and (position - 1) % 25 == 0:
            log.info("Checked %d of %d %s", position - 1, total, label)
        if previous.get("revisions", {}).get(kind, {}).get(name) == revision and name in previous[kind]:
            result[name] = previous[kind][name]
            continue
        try:
            result[name] = loader(name)
        except FrappeError as exc:
            if exc.status_code != 403:
                raise
            unavailable.append(name)
            log.debug("Metadata access denied for %s %s", kind, name)
    return result, unavailable


def _open_embedder(stack: ExitStack, site: SiteConfig):
    embedder = create_embedder(site)
    if hasattr(embedder, "close"):
        stack.callback(embedder.close)
    return embedder


def sync_metadata(site: SiteConfig, *, force: bool = False, check: bool = False) -> dict:
    """Compare revision hashes and refresh changed DocTypes, reports and vectors."""
    store = _store(site)
    packs = load_packs(site.knowledge_packs, settings.knowledge_pack_dir)
    pack_hash = _pack_hash(packs)
    log.info("Waiting for the metadata sync lock for site %s", site.name)
    with store.lock("sync"), use_site(site.name), service_purpose("indexing"):
        client = get_service_client()
        log.info("Reading the Frappe metadata catalog for site %s", site.name)
        catalog, fingerprint = _scan(site, client, pack_hash)
        active = store.active()
        old = read_json(active / "snapshot.json") if active else {}
        same = old.get("schema_hash") == fingerprint
        if check:
            return {"site": site.name, "state": "current" if same else "stale",
                    "schema_hash": fingerprint, "indexed_hash": old.get("schema_hash")}
        if same and not force:
            return _report(active, old, "unchanged")

        compatible = (
            old.get("version") == SCHEMA_VERSION and old.get("source") == _source(site)
            and old.get("indexing_account") == _indexing_account(site)
        )
        previous = old if compatible and old.get("packs") == pack_hash and not force else {}
        policy = NeverSendPolicy(packs)
        schemas, denied_types = _read_changes(
            catalog.doctypes, previous, "schemas", "DocTypes",
            lambda name: read_schema(client, name, catalog.workflows.get(name, []), policy.fields_for(name)),
        )
        reports, denied_reports = _read_changes(
            catalog.reports, previous, "reports", "reports", lambda name: read_report(client, name, policy),
        )
        log.info("Preparing metadata index cards")
        aliases, keywords = build_maps(schemas, packs)
        tiers = build_cards(schemas, reports, aliases, keywords)
        workspace = store.workspace()
        try:
            with ExitStack() as stack:
                embedder = _open_embedder(stack, site)
                previous_vectors = active if compatible and old.get("embedding") == _embedding_key(site) else None
                embedded = {}
                for tier, cards in tiers.items():
                    log.info("Building %s with %d cards using %s embeddings",
                             tier, len(cards), site.embedding_provider)
                    embedded[tier] = write_tier(workspace, tier, cards, embedder, previous_vectors)
            log.info("Rechecking the Frappe catalog before publishing the index")
            if _catalog(client, site) != catalog:
                raise MetadataError("Frappe metadata changed during indexing. Retry the sync.")
            manifest = {
                "site": site.name, "source": _source(site), "version": SCHEMA_VERSION,
                "indexing_account": _indexing_account(site),
                "schema_hash": fingerprint, "embedding": _embedding_key(site), "packs": pack_hash,
                "synced_at": datetime.now(timezone.utc).isoformat(),
                "counts": {tier: len(cards) for tier, cards in tiers.items()},
                "unavailable": {"doctypes": denied_types, "reports": denied_reports},
                "modules": catalog.modules,
                "revisions": {"schemas": catalog.doctypes, "reports": catalog.reports},
                "schemas": schemas, "reports": reports,
            }
            write_json(workspace / "snapshot.json", manifest)
            write_json(workspace / "alias_map.json", aliases)
            write_json(workspace / "business_keywords.json", keywords)
            store.publish(workspace)
        finally:
            if store.active() != workspace:
                shutil.rmtree(workspace, ignore_errors=True)
        return {**_report(workspace, manifest, "updated"), "new_embeddings": embedded}


def _manifest(active, site: SiteConfig) -> dict:
    if active is None:
        raise IndexError(f"No metadata index for {site.name}. Run sync_metadata first.")
    manifest = read_json(active / "snapshot.json")
    if manifest["source"] != _source(site) or manifest["version"] != SCHEMA_VERSION:
        raise IndexError("The Frappe source or index format changed. Run sync_metadata.")
    return manifest


def index_status(site: SiteConfig) -> dict:
    store = _store(site)
    with store.lock("snapshot"):
        active = store.active()
        return _report(active, _manifest(active, site), "ready")


@contextmanager
def open_snapshot(site: SiteConfig, packs):
    store = _store(site)
    with store.lock("snapshot"):
        active = store.active()
        manifest = _manifest(active, site)
        if manifest["embedding"] != _embedding_key(site):
            raise IndexError("Embedding configuration changed. Run sync_metadata.")
        if manifest.get("packs") != _pack_hash(packs):
            raise IndexError("Knowledge packs changed. Run sync_metadata before searching.")
        yield active, manifest


def search_metadata(site: SiteConfig, tier: str, query: str, limit: int,
                    doctype: str | None = None) -> list[dict]:
    packs = load_packs(site.knowledge_packs, settings.knowledge_pack_dir)
    with open_snapshot(site, packs) as (active, _):
        cards, index = read_tier(active, tier)
        preferred_keys = []
        if tier in {"table_index", "schema_index"}:
            preferred_keys = read_json(active / "alias_map.json").get(normalize(query), [])
        elif tier == "report_index":
            name = normalize(query)
            preferred_keys = [card["key"] for card in cards if normalize(card["key"]) == name]
    with ExitStack() as stack:
        return search_tier(cards, index, _open_embedder(stack, site), query, limit, doctype,
                           preferred_keys=preferred_keys)
