"""Vault discovery, frontmatter parsing, and per-vault mtime-cached index.

A "vault" is a ``.agent_lessons/`` directory directly under one of the working
directories (base_dir ∪ extra RO/RW). This is the first code path that treats
``.agent_lessons`` as a real location — today it is only a prompt convention.

Frontmatter parsing is deliberately best-effort and stdlib-only (naive key:value,
not a full YAML lib): on any failure we fall back to indexing the body only
(plan §9 R8). ``memory_stats.json`` sidecars are excluded from the index.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent_cascade.log import logger

# The vault directory name (convention; plan §6.3 / R7).
VAULT_DIRNAME = '.agent_lessons'

# Frontmatter identity fields we index (plan §3.3 — mirrors skill_frontmatter_text).
_IDENTITY_FIELDS = ('name', 'description', 'tags', 'aliases')

_FRONTMATTER_RE = re.compile(r'\A---\s*\n(.*?)\n---\s*\n?', re.DOTALL)

# Status values that REMOVE a lesson from the hint index (Feature A: "supersede,
# don't delete"). ALLOWLIST on purpose: only these exact values de-index. Every
# other status — the 17 pre-existing free-form values (``in-progress``, ``REMOVED
# …``), ``active``, ``deprecated``, malformed YAML — and a missing status (the
# ~402 no-frontmatter files) stay ACTIVE. De-indexed files remain on disk and
# greppable; deleting the ``status:`` line re-indexes them on the next rescan.
_DEINDEX_STATUSES = frozenset({'superseded', 'archived'})


def _is_deindexed(meta: dict) -> bool:
    """True only when ``status:`` is exactly ``superseded``/``archived``.

    A missing/non-string ``status`` returns False (active) — the required default
    for the ~402 no-frontmatter lessons and the free-form-status files alike.
    """
    st = meta.get('status', '')
    if not isinstance(st, str):
        return False
    return st.strip().lower() in _DEINDEX_STATUSES


def normalize_scope(meta: dict) -> frozenset:
    """Normalize the ``scope:`` frontmatter field into a lowercase token set.

    ``scope:`` is a comma-separated (or ``[a, b]``) list of namespace keys
    (Feature C). Absent/empty → ``{'global'}`` so an unscooped lesson is always
    eligible, mirroring the safe default for no-frontmatter files.
    """
    raw = meta.get('scope', '')
    if not isinstance(raw, str):
        return frozenset({'global'})
    tokens = {tok.strip().lower() for tok in raw.split(',') if tok.strip()}
    return frozenset(tokens) or frozenset({'global'})


def _hub_slug(meta: dict) -> Optional[str]:
    """Return the leaf's ``hub:`` cluster slug, or None if absent/blank.

    Feature B: a hinted leaf carrying ``hub: <slug>`` renders a breadcrumb to its
    hub note (`` (hub: #<slug>)``). Case is preserved — display-only.
    """
    hub = meta.get('hub', '')
    if not isinstance(hub, str):
        return None
    hub = hub.strip()
    return hub or None


def discover_vaults(om) -> List[Path]:
    """Return the ``.agent_lessons/`` dirs under base_dir + extra RO/RW folders.

    Args:
        om: An OperationManager (or any object exposing ``base_dir``,
            ``extra_work_folders_ro`` and ``extra_work_folders_rw``).

    Returns:
        De-duplicated list of vault root Paths that exist and are directories.
    """
    if om is None:
        return []
    dirs = []
    base = getattr(om, 'base_dir', None)
    if base is not None:
        dirs.append(Path(base))
    for folder in (getattr(om, 'extra_work_folders_ro', None) or []):
        dirs.append(Path(folder))
    for folder in (getattr(om, 'extra_work_folders_rw', None) or []):
        dirs.append(Path(folder))

    vaults: List[Path] = []
    seen = set()
    for d in dirs:
        try:
            vault = Path(d) / VAULT_DIRNAME
            key = str(vault).lower()
            if key in seen:
                continue
            if vault.is_dir():
                seen.add(key)
                vaults.append(vault)
        except Exception as e:  # noqa: BLE001 — discovery is best-effort
            logger.debug('[MEMORY_HINT] Vault discovery error for %s: %s', d, e)
    return vaults


def parse_frontmatter(path: Path) -> Tuple[dict, str]:
    """Parse a lesson file into ``(frontmatter_dict, body_text)``.

    Best-effort, stdlib-only. On any failure returns ``({}, full_text)`` so the
    caller can still index the body (plan §9 R8).
    """
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except Exception as e:  # noqa: BLE001 — unreadable file → body-only is empty
        logger.debug('[MEMORY_HINT] Could not read %s: %s', path, e)
        return {}, ''

    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text

    raw_block = m.group(1)
    body = text[m.end():]

    meta: dict = {}
    for line in raw_block.splitlines():
        stripped = line.strip()
        if not stripped or ':' not in stripped:
            continue
        key, _, val = stripped.partition(':')
        key = key.strip().lower()
        val = val.strip()
        # Strip surrounding quotes and inline list brackets for a clean value.
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        elif val.startswith('[') and val.endswith(']'):
            val = val[1:-1]
        meta[key] = val
    return meta, body


def identity_text(frontmatter: dict) -> str:
    """Concatenate the identity fields into one comparable string (plan §3.3)."""
    parts = []
    for field in _IDENTITY_FIELDS:
        v = frontmatter.get(field)
        if not v:
            continue
        if isinstance(v, (list, tuple)):
            v = ' '.join(str(x) for x in v)
        parts.append(str(v))
    return ' '.join(parts).strip()


class VaultIndex:
    """Holds one vault's documents plus an mtime map for targeted rebuilds.

    ``documents`` maps vault-relative path →
    ``(identity_text, body_text, scope_set, hub_slug)`` where ``scope_set`` is a
    lowercase namespace token set (Feature C, default ``{'global'}``) and
    ``hub_slug`` is the leaf's ``hub:`` cluster or ``None`` (Feature B).
    ``mtimes`` maps the same relative path → file mtime, so :meth:`rescan` can
    rebuild only changed files and drop deleted ones (plan §3.3).
    """

    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root)
        self.documents: Dict[str, Tuple[str, str, frozenset, Optional[str]]] = {}
        self.mtimes: Dict[str, float] = {}

    def rescan(self) -> bool:
        """(Re)build the index for this vault. Returns True if anything changed."""
        new_docs: Dict[str, Tuple[str, str, frozenset, Optional[str]]] = {}
        new_mtimes: Dict[str, float] = {}
        try:
            files = sorted(self.vault_root.rglob('*.md'))
        except Exception as e:  # noqa: BLE001
            logger.debug('[MEMORY_HINT] rglob failed for %s: %s', self.vault_root, e)
            files = []

        for f in files:
            if not f.is_file():
                continue
            rel = f.relative_to(self.vault_root).as_posix()
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            # Skip the stats sidecar (it's JSON, but guard anyway) and hidden files.
            if rel == 'memory_stats.json' or rel.startswith('.'):
                continue
            meta, body = parse_frontmatter(f)
            # Feature A: a superseded/archived lesson stays on disk (greppable) but is
            # dropped from the hint index. Allowlist-only, so every other status and
            # the no-frontmatter default stay active.
            if _is_deindexed(meta):
                continue
            new_docs[rel] = (identity_text(meta), body, normalize_scope(meta), _hub_slug(meta))
            new_mtimes[rel] = mtime

        changed = (new_docs != self.documents) or (new_mtimes != self.mtimes)
        if changed:
            self.documents = new_docs
            self.mtimes = new_mtimes
        return changed

    def all_vaults(self, vault_indexes: Dict[Path, 'VaultIndex']) -> Dict[str, Tuple[str, str, frozenset, Optional[str]]]:
        """Return the union of documents across all vaults (for the matcher).

        Keys are BARE vault-relative paths so they agree with the dedup set, cooldown
        map and stats sidecar (plan §3.1/§3.4). Cross-vault filename collisions resolve
        to first-vault-wins; that is acceptable for this feature's small vault sizes.
        """
        merged: Dict[str, Tuple[str, str, frozenset, Optional[str]]] = {}
        for _root, idx in vault_indexes.items():
            for rel, doc in idx.documents.items():
                # First vault wins on collision; deterministic (sorted insertion).
                merged.setdefault(rel, doc)
        return merged


def get_memory_identity(vault_root: Path, rel_path: str) -> str:
    """Canonical memory identity for a lesson under ``vault_root``.

    Mirrors :meth:`VaultIndex.all_vaults` keying (bare vault-relative path) so the
    read-tracking hook and the matcher agree on which memory a path refers to.
    """
    return rel_path


def is_under_vault(resolved: Path, vault_roots: List[Path]) -> Optional[Tuple[Path, str]]:
    """Return ``(vault_root, vault_rel_path)`` if ``resolved`` is a lesson under a vault.

    Returns None when the path is not under any known vault (or isn't a file).
    """
    resolved = Path(resolved)
    for root in vault_roots:
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        if rel.parts and not any(p.startswith('.') for p in rel.parts):
            return root, rel.as_posix()
    return None
