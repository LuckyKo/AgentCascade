#!/usr/bin/env python3
"""P1 file-mutation safety tests (edit_file + write_file commit-region lock).

Covers the P1 acceptance contract:

  * (a) Two concurrent same-file edits from two threads serialize with no lost
    update and no clobber — the final bytes always equal the original plus exactly
    the set of edits that reported success; no successful write is silently lost and
    no stale write lands.
  * (b) An agent parked on approval does not block a second writer on the same path
    — the mutation lock wraps only the commit region, never the approval wait.
  * (c) An external write between the pre-commit snapshot and the write is refused:
    ``edit_file`` (regression of the existing raw-byte guard) and ``write_file`` (new
    mtime+size baseline guard on the overwrite branch).

Plus the deliberate write_file correctness fixes: the new-file branch has no
baseline (unguarded), and writes are verbatim so an LF file is not flipped to CRLF
on a Windows host — for both an overwrite and a brand-new file.

Run: python -m pytest tests/test_edit_file_mutation_p1.py -q
"""

import tempfile
import threading
import time
from pathlib import Path

from agent_cascade.operation_manager import OperationManager


def _make_om(tmpdir):
    om = OperationManager(base_dir=str(tmpdir))
    om.file_ownership = {}
    return om


def _owned_file(om, tmpdir, name, content):
    f = Path(tmpdir, name)
    f.write_text(content, encoding='utf-8')
    om._own(f.resolve(), 'coder')
    return f


# ── external-write guard (acceptance c) ───────────────────────────────────────


def test_edit_file_external_write_guard_regression(monkeypatch):
    """Regression: the raw-byte guard still aborts after the commit lock was added.

    The file is not agent-owned, so the edit parks on approval; an external writer
    changes it during that window. The stale edit must abort with no write, no
    backup, and the external work preserved.
    """
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, 'conc_edit.txt')
        f.write_text('alpha\nbeta\n', encoding='utf-8')

        def racy_approval(**_kwargs):
            f.write_text('external work\n', encoding='utf-8')
            return True, 'approved'

        monkeypatch.setattr(om, 'request_user_approval', racy_approval)
        res = om.edit_file(str(f), 'coder', old_content='beta\n',
                           new_content='beta2\n', match_mode='exact')
        assert res.startswith('ERROR') and 'concurrent' in res.lower(), res
        assert f.read_text(encoding='utf-8') == 'external work\n', \
            f"concurrent writer's content was clobbered: {f.read_text(encoding='utf-8')!r}"
        assert not list(Path(d).rglob('*.bak')), 'no backup should be created on abort'


def test_write_file_overwrite_guard_refuses_on_drift(monkeypatch):
    """New guard: a changed target after the pre-approval snapshot is refused.

    The overwrite captures an mtime+size baseline before the approval wait; an
    external writer during that window must abort the write (no clobber, no backup).
    """
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, 'conc_write.txt')
        f.write_text('alpha\nbeta\n', encoding='utf-8')  # not owned → goes through approval

        def racy_approval(**_kwargs):
            f.write_text('external work that is longer\n', encoding='utf-8')
            return True, 'approved'

        monkeypatch.setattr(om, 'request_user_approval', racy_approval)
        res = om.write_file(str(f), 'replacement\n', 'coder')
        assert res.startswith('ERROR') and 'concurrent' in res.lower(), res
        assert f.read_text(encoding='utf-8') == 'external work that is longer\n', \
            f"concurrent writer's content was clobbered: {f.read_text(encoding='utf-8')!r}"
        assert not list(Path(d).rglob('*.bak')), 'no backup should be created on abort'


def test_write_file_new_file_is_unguarded(monkeypatch):
    """A brand-new file has no baseline, so it is correctly not guarded.

    No mtime+size snapshot exists for a file that does not yet exist; the write
    proceeds and succeeds.
    """
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, 'brand_new.txt')
        res = om.write_file(str(f), 'hello\n', 'coder')
        assert res.startswith('OK'), res
        assert f.read_text(encoding='utf-8') == 'hello\n'


# ── verbatim line-ending round-trip (deliberate correctness fix) ──────────────


def test_write_file_overwrite_preserves_lf_endings():
    """An LF overwrite stays LF — newline='' disables OS translation.

    The old ``write_text`` (default newline handling) flipped LF to CRLF on a
    Windows host; the verbatim write must not, on any platform.
    """
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'lf_overwrite.txt', 'old line1\nold line2\n')
        res = om.write_file(str(f), 'new line1\nnew line2\n', 'coder')
        assert res.startswith('OK'), res
        raw = f.read_bytes()
        assert raw == b'new line1\nnew line2\n', \
            f"LF overwrite was not verbatim: {raw!r}"


def test_write_file_new_file_preserves_lf_endings():
    """A new file written with LF content stays LF — no style detection involved."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, 'lf_new.txt')
        res = om.write_file(str(f), 'first\nsecond\nthird\n', 'coder')
        assert res.startswith('OK'), res
        raw = f.read_bytes()
        assert raw == b'first\nsecond\nthird\n', \
            f"LF new-file write was not verbatim: {raw!r}"


# ── concurrency: serialize, no lost update / no clobber (acceptance a) ────────


def test_two_concurrent_same_file_edits_no_lost_update_or_clobber():
    """Two threads edit disjoint lines of the same owned file concurrently.

    The commit lock serializes the critical section. Under every interleaving the
    result must equal the original plus exactly the set of edits that reported OK:
    a thread whose change is not present reported a clean drift-abort, never a
    silent loss, and no stale bytes were written over concurrent work.
    """
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        original = 'a\nb\nc\nd\n'
        f = _owned_file(om, d, 'race.txt', original)  # owned → both auto-approved, no approval wait

        # (label, old, new) — disjoint single-line replacements.
        jobs = {
            'edit_line1': ('a\n', 'A1\n'),
            'edit_line2': ('b\n', 'B2\n'),
        }
        barrier = threading.Barrier(len(jobs))
        results = {}
        errors = {}

        def worker(label, old, new):
            barrier.wait()  # release both threads together to maximize the race window
            try:
                results[label] = om.edit_file(str(f), 'coder',
                                              old_content=old, new_content=new,
                                              match_mode='exact')
            except Exception as e:  # a crash is a hard failure of the lock discipline
                errors[label] = repr(e)

        threads = [threading.Thread(target=worker, args=(lbl, old, new))
                   for lbl, (old, new) in jobs.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"worker raised: {errors}"
        assert not any(t.is_alive() for t in threads), 'a worker did not finish (deadlock?)'

        ok = [lbl for lbl, r in results.items() if r.startswith('OK')]
        aborted = [lbl for lbl, r in results.items() if 'changed on disk' in r.lower()]
        # Every thread reported cleanly: OK (it landed) or a drift-abort (it did not).
        assert len(ok) + len(aborted) == len(jobs), f"unclean results: {results}"
        # The first writer always finds pristine bytes, so at least one edit lands.
        assert ok, f"no edit applied; results={results}"

        # The final bytes must equal the original with exactly the OK edits applied.
        expected = original
        for lbl in ok:
            old, new = jobs[lbl]
            assert expected.count(old) == 1, 'anchor no longer unique in expected state'
            expected = expected.replace(old, new, 1)
        assert f.read_text(encoding='utf-8') == expected, (
            f"final file does not match original + OK edits; "
            f"final={f.read_text(encoding='utf-8')!r} expected={expected!r} results={results}")


# ── concurrency: write_file serialize, no lost update / no clobber ──────────────


def test_two_concurrent_same_file_writes_no_lost_update_or_clobber():
    """Two threads overwrite the same owned file concurrently (write_file path).

    Mirrors the edit_file 2-thread test. The commit lock serializes the critical
    section, and each thread snapshots its own mtime+size baseline before
    contending. The dominant race has thread A commit first (its baseline matches
    the pristine file) and thread B then see A's newer bytes against its stale
    baseline and abort on drift; under a slower interleaving B can snapshot after
    A lands and both succeed sequentially. Under every interleaving the committed
    bytes are the last OK writer's — no silent clobber of a landed write, and every
    non-landing writer reports a clean drift-abort, never a silent loss.
    """
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'race_write.txt', 'original line\n')  # owned → auto-approved

        # Distinct full-file contents; a whole-file overwrite makes the final bytes
        # identify exactly which write landed last.
        jobs = {
            'write_x': 'x1\nx2\n',
            'write_y': 'y1\ny2\ny3\n',
        }
        barrier = threading.Barrier(len(jobs))
        order = threading.Lock()
        counter = [0]
        results = {}   # label -> (result_str, completion_index)
        errors = {}

        def worker(label, content):
            barrier.wait()  # release both threads together to maximize the race window
            try:
                r = om.write_file(str(f), content, agent_name='coder')
                with order:
                    counter[0] += 1
                    idx = counter[0]
                results[label] = (r, idx)
            except Exception as e:  # a crash is a hard failure of the lock discipline
                errors[label] = repr(e)

        threads = [threading.Thread(target=worker, args=(lbl, c))
                   for lbl, c in jobs.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"worker raised: {errors}"
        assert not any(t.is_alive() for t in threads), 'a worker did not finish (deadlock?)'

        ok = [lbl for lbl, (r, _) in results.items() if r.startswith('OK')]
        aborted = [lbl for lbl, (r, _) in results.items() if 'changed on disk' in r.lower()]
        # Every thread reported cleanly: OK (it may have landed) or a drift-abort (it did not).
        assert len(ok) + len(aborted) == len(jobs), f"unclean results: {results}"
        # The first writer always finds the pristine file, so at least one write lands.
        assert ok, f"no write applied; results={results}"

        # The committed bytes are exactly the last OK writer's content: the last write
        # is present (no lost update), and no landed write was silently overwritten.
        last_ok = max(ok, key=lambda lbl: results[lbl][1])
        assert f.read_text(encoding='utf-8') == jobs[last_ok], (
            f"final file is not the last OK writer's content; "
            f"final={f.read_text(encoding='utf-8')!r} expected={jobs[last_ok]!r} "
            f"results={results}")


# ── no deadlock across a parked approval (acceptance b) ───────────────────────


def test_parked_approval_does_not_block_second_writer(monkeypatch):
    """A writer parked on approval must not block a second writer on the same path.

    The mutation lock wraps only the commit region, not the unbounded approval wait.
    While agent A sits in ``request_user_approval`` (never having taken the lock),
    owner agent B edits the same file to completion without blocking — then A is
    released and its now-stale edit aborts on drift.
    """
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = _owned_file(om, d, 'park.txt', 'a\nb\n')  # owned by 'coder'

        parked = threading.Event()
        release = threading.Event()

        def blocking_approval(**_kwargs):
            parked.set()
            release.wait(timeout=10)
            return True, 'approved'

        # Only the not-owned caller ('agentA') reaches approval; 'coder' is auto-approved.
        monkeypatch.setattr(om, 'request_user_approval', blocking_approval)

        a_state = {}

        def agent_a():
            a_state['res'] = om.edit_file(str(f), 'agentA', old_content='a\n',
                                          new_content='A\n', match_mode='exact')
            a_state['done'] = True

        ta = threading.Thread(target=agent_a)
        ta.start()

        assert parked.wait(timeout=5), 'agent A did not park on approval'

        # A is parked (in approval, no lock held). B — auto-approved — edits the SAME
        # path and must finish promptly, proving the lock is not held across approval.
        start = time.perf_counter()
        b_res = om.edit_file(str(f), 'coder', old_content='a\n',
                             new_content='B\n', match_mode='exact')
        elapsed = time.perf_counter() - start
        assert elapsed < 4, f"B blocked on A's parked approval ({elapsed:.2f}s)"
        assert b_res.startswith('OK'), f"B edit failed: {b_res}"
        assert not a_state.get('done'), 'A should still be parked on approval'

        # Release A. It re-reads under the lock, sees B's change, and aborts on drift.
        release.set()
        ta.join(timeout=10)
        assert not ta.is_alive(), 'agent A did not finish after approval release'
        assert 'changed on disk' in a_state['res'].lower(), \
            f"stale post-park edit should abort on drift: {a_state['res']}"
        assert f.read_text(encoding='utf-8') == 'B\nb\n', \
            f"B's committed change was not preserved: {f.read_text(encoding='utf-8')!r}"
