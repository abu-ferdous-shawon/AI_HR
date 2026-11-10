import sys

from collections import Counter

from app.tools.hr.policy import get_index

index = get_index()
index.refresh()
print("Folder:", index.folder, "" if index.folder.is_dir() else " <-- DOES NOT EXIST")

counts = Counter(chunk.source for chunk in index._chunks)
files = index._files()
if not files:
    print("No .txt, .md, .pdf or .docx files found in that folder.")
for path in files:
    n = counts.get(path.name, 0)
    note = "" if n else "   <-- NOTHING READ (scanned PDF, or pypdf / python-docx missing?)"
    print(f"  {path.name}: {n} searchable pieces{note}")

query = " ".join(sys.argv[1:])
if query:
    print(f"\nSearch for: {query}")
    for hit in index.search(query):
        print(f"\n[{hit['source']}]\n{hit['text'][:300]}")