"""BUG_0221: list_dir prunes VCS dirs even when `include` explicitly names them.

Two defects:
1. An explicit `include=".git"` could not re-admit the `.git` directory over the
   default `skip_vcs=True` prune, so a name-scoped sweep silently omitted it.
2. The pruning was disclosed only on empty results; a non-empty listing carried
   no note that a VCS category was pruned.

Fix:
- `include` patterns that name a pruned category (`.git`, `.git/*`, …) re-admit
  the directory over `skip_vcs`.
- A disclosure note appears whenever `vcs_pruned > 0` AND the caller's
  include/exclude patterns name a pruned category (not only on empty results).
"""

from agent_cascade.operation_manager import OperationManager


def test_include_git_readmits_vcs_dir(tmp_path):
    """include='.git' re-admits the .git directory over the default skip_vcs prune."""
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'HEAD').write_text('ref: refs/heads/main', encoding='utf-8')
    (tmp_path / 'a.txt').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, include='.git', char_limit=-1)

    assert '.git' in out, f".git must appear when include names it: {out}"


def test_include_git_glob_readmits_vcs_dir(tmp_path):
    """include='.git/*' (subtree glob) also re-admits the .git directory."""
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'HEAD').write_text('ref: refs/heads/main', encoding='utf-8')
    (tmp_path / 'a.txt').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, include='.git/*', char_limit=-1)

    assert '.git' in out, f".git must appear when include globs its subtree: {out}"


def test_plain_recursive_still_prunes_vcs(tmp_path):
    """A plain recursive listing WITHOUT an include naming a VCS dir still prunes .git."""
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'HEAD').write_text('ref: refs/heads/main', encoding='utf-8')
    (tmp_path / 'node_modules').mkdir()
    (tmp_path / 'node_modules' / 'pkg.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'keep.py').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, char_limit=-1)

    assert '.git' not in out, f".git must be pruned by default: {out}"
    assert 'node_modules' not in out, f"node_modules must be pruned by default: {out}"
    assert 'keep.py' in out, f"keep.py must still be listed: {out}"


def test_no_disclosure_note_when_nothing_pruned(tmp_path):
    """The disclosure note must NOT appear when no VCS dir was pruned (BUG_0166 invariant)."""
    (tmp_path / 'a.txt').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, include='.git', char_limit=-1)

    assert 'pruned by skip_vcs' not in out, f"no note when nothing was pruned: {out}"


def test_disclosure_note_when_vcs_pruned_and_named(tmp_path):
    """The disclosure note appears when a VCS dir IS pruned AND the caller named it."""
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'HEAD').write_text('ref: refs/heads/main', encoding='utf-8')
    (tmp_path / 'node_modules').mkdir()
    (tmp_path / 'node_modules' / 'pkg.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'keep.py').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    # include names .git (which gets re-admitted), but node_modules is still pruned.
    out = om.list_directory('.', recursive=True, include='.git', char_limit=-1)

    assert '.git' in out, f".git must be re-admitted: {out}"
    assert 'node_modules' not in out, f"node_modules must still be pruned: {out}"
    assert 'pruned by skip_vcs' in out, f"disclosure note must appear: {out}"


def test_disclosure_note_with_exclude_naming_vcs(tmp_path):
    """The disclosure note also fires when exclude names a pruned category."""
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'HEAD').write_text('ref: refs/heads/main', encoding='utf-8')
    (tmp_path / 'node_modules').mkdir()
    (tmp_path / 'node_modules' / 'pkg.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'keep.py').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, exclude='node_modules', char_limit=-1)

    assert 'node_modules' not in out, f"node_modules must be excluded: {out}"
    assert 'pruned by skip_vcs' in out, f"disclosure note must appear: {out}"


def test_skip_vcs_false_still_works(tmp_path):
    """skip_vcs=False restores all VCS dirs regardless of include (regression guard)."""
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'HEAD').write_text('ref: refs/heads/main', encoding='utf-8')
    (tmp_path / 'keep.py').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, skip_vcs=False, char_limit=-1)

    assert '.git' in out, f".git must appear when skip_vcs=False: {out}"
    assert 'pruned by skip_vcs' not in out, f"no note when skip_vcs is off: {out}"


def test_catch_all_include_still_prunes_vcs(tmp_path):
    """include='*' is a catch-all, not a specific VCS name — .git must still be pruned."""
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'HEAD').write_text('ref: refs/heads/main', encoding='utf-8')
    (tmp_path / 'keep.py').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, include='*', char_limit=-1)

    assert '[DIR] .git/' not in out, f"catch-all include='*' must not re-admit .git: {out}"
    assert 'keep.py' in out, f"keep.py must still be listed: {out}"


def test_exclude_wins_over_include_readmission(tmp_path):
    """include='.git' + exclude='.git' → exclude wins, .git is pruned."""
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'HEAD').write_text('ref: refs/heads/main', encoding='utf-8')
    (tmp_path / 'keep.py').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, include='.git', exclude='.git', char_limit=-1)

    assert '[DIR] .git/' not in out, f"exclude='.git' must win over include re-admission: {out}"


def test_catch_all_include_with_exclude_still_prunes(tmp_path):
    """include='*' + exclude='.git' → .git is pruned (catch-all doesn't re-admit)."""
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'HEAD').write_text('ref: refs/heads/main', encoding='utf-8')
    (tmp_path / 'keep.py').write_text('x', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, include='*', exclude='.git', char_limit=-1)

    assert '[DIR] .git/' not in out, f".git must be pruned by exclude: {out}"
    assert 'keep.py' in out, f"keep.py must be listed: {out}"


def test_nested_git_at_depth2(tmp_path):
    """Nested .git at depth 2 is re-admitted by include='.git' (mirrors bug report)."""
    (tmp_path / 'sub').mkdir()
    (tmp_path / 'sub' / '.git').mkdir()
    (tmp_path / 'sub' / '.git' / 'HEAD').write_text('ref: refs/heads/main', encoding='utf-8')
    (tmp_path / 'sub' / 'file.txt').write_text('x', encoding='utf-8')
    (tmp_path / 'top.txt').write_text('y', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    out = om.list_directory('.', recursive=True, include='.git', max_depth=2, char_limit=-1)

    assert 'sub/.git' in out, f"nested .git must appear: {out}"


def test_disclosure_note_survives_char_limit(tmp_path):
    """The disclosure note is in the header region and survives head-truncation."""
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'HEAD').write_text('ref: refs/heads/main', encoding='utf-8')
    (tmp_path / 'node_modules').mkdir()
    (tmp_path / 'node_modules' / 'pkg.json').write_text('{}', encoding='utf-8')
    # Create enough .txt files (matched by include) to exceed the default char_limit=3000.
    for i in range(100):
        (tmp_path / f'file_{i:03d}.txt').write_text(f'content {i}', encoding='utf-8')
    om = OperationManager(base_dir=str(tmp_path))

    # include='.git,*.txt' re-admits .git AND lists the .txt files, making the
    # output long enough to trigger head-truncation at the default char_limit=3000.
    out = om.list_directory('.', recursive=True, include='.git,*.txt', max_entries=200)

    assert 'TRUNCATED' in out, (
        f"listing must be truncated to prove the note survives: {out[-200:]}")
    assert 'pruned by skip_vcs' in out, (
        f"disclosure note must survive char_limit truncation: {out[:200]}...")
