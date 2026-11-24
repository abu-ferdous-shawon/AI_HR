import sys

from app.core import sites

for site in sites.get_sites().values():
    print(f"{site.name:12} {site.frappe_url}  hosts={site.hosts or 'any'}\n"
          f"{'':12} llm={site.llm_provider}/{site.llm_model}  "
          f"embeddings={site.embedding_provider}/{site.embedding_model}\n"
          f"{'':12} packs={site.knowledge_packs}  indexing={site.indexing_schedule}")
print("default:", sites.get_default_site().name)
for host in sys.argv[1:]:
    print(f"Host {host!r} -> site {sites.site_for_host(host).name}")