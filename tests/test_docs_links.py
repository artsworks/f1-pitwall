"""Doc links resolve: the README index, the docs site navigation and links between docs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
# The docs site links code spans such as `03-strategy.md` and `adr/0002-two-clock-domains.md`.
CODE_DOC = re.compile(r"`(?:docs/)?((?:reference/|adr/)?\d{2,4}-[\w-]+\.md)`")
SITE_GROUPS = re.compile(r"^const GROUPS = \[$(.*?)^\];$", re.MULTILINE | re.DOTALL)
SITE_ID = re.compile(r'^\s*\["([\w/-]+)", "[^"]*", "', re.MULTILINE)


def _local_targets(text: str) -> list[str]:
    targets = []
    for target in MD_LINK.findall(text):
        if re.match(r"^[a-z]+:", target, re.IGNORECASE) or target.startswith("#"):
            continue
        targets.append(target.split("#", 1)[0])
    return [t for t in targets if t]


def _missing(base: Path, targets: list[str]) -> list[str]:
    return [t for t in targets if not (base / t).exists()]


def test_readme_links_exist() -> None:
    assert _missing(ROOT, _local_targets((ROOT / "README.md").read_text("utf-8"))) == []


def test_docs_site_entries_exist() -> None:
    groups = SITE_GROUPS.search((DOCS / "index.html").read_text("utf-8"))
    assert groups
    ids = SITE_ID.findall(groups.group(1))
    assert ids
    assert [i for i in ids if not (DOCS / f"{i}.md").exists()] == []


@pytest.mark.parametrize("doc", sorted(DOCS.rglob("*.md")), ids=lambda p: str(p.relative_to(DOCS)))
def test_doc_links_exist(doc: Path) -> None:
    text = doc.read_text("utf-8")
    missing = _missing(doc.parent, _local_targets(text))
    missing += [
        ref
        for ref in CODE_DOC.findall(text)
        if not (doc.parent / ref).exists() and not (DOCS / ref).exists()
    ]
    assert missing == []
