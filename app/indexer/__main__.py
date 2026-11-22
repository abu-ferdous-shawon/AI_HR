"""Run metadata indexing as an OS-authorized administrative command."""
import argparse
import json
import logging
import time

from app.core.sites import get_default_site, get_sites
from app.frappe_client.client import FrappeError
from app.indexer.cards import TIERS
from app.indexer.sync import index_status, search_metadata, sync_metadata

log = logging.getLogger("hr_ai.indexing")


def _emit(result) -> None:
    print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)


def _watch(sites) -> None:
    intervals = {site.name: int(site.indexing_schedule.split(":")[1]) * 3600
                 for site in sites if site.indexing_schedule.startswith("every:")}
    if not intervals:
        raise ValueError("Set INDEXING_SCHEDULE or the site's indexing_schedule to every:<hours>")
    due = dict.fromkeys(intervals, time.monotonic())
    while True:
        for site in sites:
            if site.name not in due or due[site.name] > time.monotonic():
                continue
            try:
                _emit(sync_metadata(site))
            except (FrappeError, ValueError, RuntimeError, OSError) as exc:
                log.error("%s metadata sync failed: %s", site.name, _error(exc))
            due[site.name] = time.monotonic() + intervals[site.name]
        time.sleep(max(0, min(due.values()) - time.monotonic()))


def _error(exc: Exception) -> str:
    if isinstance(exc, FrappeError):
        return (f"Frappe returned HTTP {exc.status_code} while reading metadata"
                if exc.status_code else "Could not connect to Frappe")
    return str(exc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Administer Frappe metadata indexes.")
    parser.add_argument("--site", help="Configured site name. Defaults to the default site.")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync_metadata", help="Refresh the site metadata and vector indexes")
    options = sync.add_mutually_exclusive_group()
    options.add_argument("--check", action="store_true", help="Only check freshness. Exit 1 when stale.")
    options.add_argument("--force", action="store_true", help="Reload metadata even if revisions match")
    commands.add_parser("status", help="Show the active snapshot and card counts")
    commands.add_parser("watch", help="Sync all configured scheduled sites, or one with --site")
    search = commands.add_parser("search", help="Inspect search results from a metadata index")
    search.add_argument("tier", choices=TIERS)
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--doctype", help="Restrict field_index results to this DocType")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        sites = get_sites()
        if args.site is not None and args.site not in sites:
            raise ValueError(f"Unknown site {args.site!r}. Configured sites: {', '.join(sites)}")
        site = sites[args.site] if args.site else get_default_site()
        if args.command == "watch":
            _watch([site] if args.site else list(sites.values()))
        elif args.command == "sync_metadata":
            result = sync_metadata(site, force=args.force, check=args.check)
            _emit(result)
            return int(result["state"] == "stale")
        elif args.command == "status":
            _emit(index_status(site))
        elif args.command == "search":
            if args.doctype and args.tier != "field_index":
                raise ValueError("--doctype is only available for field_index")
            _emit(search_metadata(site, args.tier, args.query, args.limit, args.doctype))
    except KeyboardInterrupt:
        return 130
    except (FrappeError, ValueError, RuntimeError, OSError) as exc:
        log.error("%s", _error(exc))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
