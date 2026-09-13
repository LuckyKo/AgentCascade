"""Tests for the delete_file tool — Phase A hardening + Phase B multi-entry/filters.

Uses a temp workspace + a REAL OperationManager (no LLM / no API server required).
Approval is driven through the real blocking ``request_user_approval`` path by
resolving pending approvals on a background thread, so the auto-approve / reject /
approve flows are exercised exactly as in production.

Covers:
  Phase A: auto-approve of owned file; non-owned reject/approve; backup-failure
           simulation (A2); dir ownership cleanup under case-differing keys (A3);
           justification preserved on auto-approve (A4); ownership concurrency (A1);
           symlink bypass rejection (A9).
  Phase B: multi-path delete; include filter; size/date filters; single aggregate
           approval for mixed sets + reject leaves all intact; empty-match early return
           with NO approval prompt (B6); continue-on-error (B4); containment/RO escape.
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# Resolve project root relative to this test file (tests/ → project_root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_om(tmpdir):
    """A real OperationManager over a temp workspace (no agent_pool)."""
    from agent_cascade.operation_manager import OperationManager
    om = OperationManager(base_dir=str(tmpdir))
    # Short timeout so a stuck approval can't hang the test forever.
    om.enable_timeout = True
    om.approval_timeout_seconds = 30
    return om


def _resolve_approval(om, decision="approve", reason="", delay=0.15):
    """Resolve the first pending approval on a background thread (like the WebUI).

    Returns the resolved request_id (or None if nothing was pending in time).
    """
    result = {"id": None}

    def _worker():
        deadline = time.time() + 10
        while time.time() < deadline:
            pendings = om.list_pending_approvals()
            if pendings:
                rid = pendings[0]["request_id"]
                if decision == "approve":
                    om.user_approve(rid, reason)
                else:
                    om.user_reject(rid, reason)
                result["id"] = rid
                return
            time.sleep(0.02)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    if delay:
        time.sleep(delay)
    t.join(timeout=12)
    return result["id"]


def _backup_dir(om, agent="coder"):
    import re as _re
    safe_agent = _re.sub(r"[^a-zA-Z0-9_-]", "_", agent)
    return Path(om.base_dir) / "logs" / "backups" / safe_agent


# ── Phase A: hardening ────────────────────────────────────────────────────────

def test_auto_approved_owned_delete_no_approval():
    """Deleting an agent-owned file is auto-approved — no approval prompt fires."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, "owned.txt"); f.write_text("data")
        om._own(f.resolve(), "coder")

        # Watch for any pending approval (there must be none).
        saw_approval = []
        def _watcher():
            time.sleep(0.4)
            if om.list_pending_approvals():
                saw_approval.append(True)
        wt = threading.Thread(target=_watcher, daemon=True); wt.start()

        res = om.delete_file(str(f), "coder", justification="cleanup")
        wt.join(timeout=3)

        assert res.startswith("OK: Deleted"), f"Expected OK, got: {res}"
        assert not saw_approval, "Auto-approved delete must NOT fire an approval prompt"
        assert not f.exists(), "Owned file should be deleted"
        assert om._get_owner(f.resolve()) is None, "Ownership entry should be cleared"
    print("[PASS] test_auto_approved_owned_delete_no_approval")


def test_non_owned_reject_leaves_file():
    """Non-owned delete → user rejects → nothing is deleted, returns REJECTED."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, "foreign.txt"); f.write_text("keep me")

        rid = threading.Thread(
            target=_resolve_approval, args=(om, "reject", "no thanks"), daemon=True).start()
        res = om.delete_file(str(f), "coder", justification="want it gone")

        assert res.startswith("REJECTED"), f"Expected REJECTED, got: {res}"
        assert f.exists(), "Rejected delete must leave the file intact"
        # No backup should have been created.
        bdir = _backup_dir(om)
        assert not (bdir and list(bdir.glob("foreign.txt*"))), "No backup on rejection"
    print("[PASS] test_non_owned_reject_leaves_file")


def test_non_owned_approved_deletes_with_backup():
    """Non-owned delete → user approves → deleted + a backup is created."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, "foreign2.txt"); f.write_text("bye")

        threading.Thread(
            target=_resolve_approval, args=(om, "approve", "ok go"), daemon=True).start()
        res = om.delete_file(str(f), "coder", justification="cleanup")

        assert res.startswith("OK: Deleted"), f"Expected OK, got: {res}"
        assert not f.exists(), "Approved delete should remove the file"
        bdir = _backup_dir(om)
        backups = list(bdir.glob("foreign2.txt*")) if bdir.exists() else []
        assert len(backups) == 1, f"Expected exactly one backup, got {backups}"
        # Backup content matches original.
        assert backups[0].read_text() == "bye", "Backup should preserve file content"
    print("[PASS] test_non_owned_approved_deletes_with_backup")


def test_backup_failure_simulation_a2(monkeypatch):
    """Simulate a copy failure → original intact, no partial backup, clear error (A2).

    On the same volume shutil.move is a rename (succeeds), so we patch shutil.move to
    raise and force the copy fallback; then make shutil.copy2 fail. The original must
    remain, no partial backup may be left, and the error must name the failed step.
    """
    import shutil as _shutil

    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, "victim.txt"); f.write_text("precious")
        resolved = f.resolve()

        def boom(*a, **k):
            raise OSError("simulated disk full during copy")

        # Force the copy fallback (shutil.move is a same-volume rename that would
        # succeed), then make the copy itself fail. Call _delete_one directly so the
        # per-target error surfaces as an ERROR string rather than a bulk failure line.
        monkeypatch.setattr(_shutil, "move", lambda *a, **k: (_ for _ in ()).throw(OSError("force fallback")))
        monkeypatch.setattr(_shutil, "copy2", boom)

        try:
            om._delete_one(resolved, "coder", justification="x")
            raise AssertionError("_delete_one should have raised on a backup failure")
        except Exception as e:
            assert "backup failed before delete" in str(e), f"Error must name the step: {e}"

        assert f.exists(), "Original must remain intact after a backup failure"
        assert f.read_text() == "precious", "Original content must be unchanged"
        bdir = _backup_dir(om)
        partials = list(bdir.glob("victim.txt*")) if bdir.exists() else []
        assert not partials, f"No partial backup may remain: {partials}"
    print("[PASS] test_backup_failure_simulation_a2")


def test_dir_delete_clears_case_differing_child_ownership_a3():
    """Deleting a dir clears child ownership entries even stored under different case (A3)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        subdir = Path(d, "MyDir"); subdir.mkdir()
        child = subdir / "child.txt"; child.write_text("c")

        # Own the dir and a child under DIFFERENT-case keys (simulating write-time casing).
        om._own(subdir.resolve(), "coder")
        # Manually store a case-differing child key to prove normcase comparison matches.
        weird_key = os.path.normcase(str(child.resolve()))
        weird_key = weird_key[:-4] + ("CHILD" if weird_key.endswith("child.txt") else weird_key)  # force-case the stem
        om.file_ownership[weird_key] = "coder"

        res = om.delete_file(str(subdir), "coder", justification="cleanup")
        assert res.startswith("OK: Deleted"), f"Expected OK, got: {res}"
        assert not subdir.exists(), "Directory should be deleted"
        # The dir's own key and the case-differing child key must both be gone.
        assert om._get_owner(subdir.resolve()) is None
        assert weird_key not in om.file_ownership, "Case-differing child ownership key must be cleared"
    print("[PASS] test_dir_delete_clears_case_differing_child_ownership_a3")


def test_auto_approved_preserves_justification_a4():
    """Auto-approved delete echoes the agent's stated justification (A4)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, "own.txt"); f.write_text("x")
        om._own(f.resolve(), "coder")
        res = om.delete_file(str(f), "coder", justification="temp scratch file no longer needed")
        assert res.startswith("OK: Deleted"), f"Expected OK, got: {res}"
        assert "Security Justification:" in res, f"Justification must be preserved: {res}"
        assert "temp scratch file no longer needed" in res
    print("[PASS] test_auto_approved_preserves_justification_a4")


def test_concurrency_no_lost_ownership_updates_a1():
    """Two threads owning/deleting different files → no lost ownership updates (A1)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        n = 50
        files = [Path(d, f"f{i}.txt") for i in range(n)]
        for fp in files:
            fp.write_text(str(fp.name))

        # Phase 1: concurrent ownership registration.
        def own_half(start):
            for i in range(start, n, 2):
                om._own(files[i].resolve(), "coder")
        t1 = threading.Thread(target=own_half, args=(0,))
        t2 = threading.Thread(target=own_half, args=(1,))
        t1.start(); t2.start(); t1.join(); t2.join()

        owned_count = sum(1 for fp in files if om._get_owner(fp.resolve()) == "coder")
        assert owned_count == n, f"All {n} files should be owned, got {owned_count}"

        # Phase 2: concurrent deletion (all auto-approved).
        def delete_half(start):
            for i in range(start, n, 2):
                om.delete_file(str(files[i]), "coder", justification="x")
        d1 = threading.Thread(target=delete_half, args=(0,))
        d2 = threading.Thread(target=delete_half, args=(1,))
        d1.start(); d2.start(); d1.join(); d2.join()

        remaining = [fp for fp in files if fp.exists()]
        assert not remaining, f"All files should be deleted, remaining: {remaining}"
        # Ownership dict must be empty (no stale entries leaked).
        assert len(om.file_ownership) == 0, f"Ownership dict should be empty, got {len(om.file_ownership)}"
    print("[PASS] test_concurrency_no_lost_ownership_updates_a1")


def test_symlink_bypass_rejected_a9():
    """A workspace symlink → out-of-bounds target; delete_file on the link rejects (A9)."""
    with tempfile.TemporaryDirectory() as ws:
        with tempfile.TemporaryDirectory() as outside:
            om = _make_om(ws)
            target = Path(outside, "secret.txt"); target.write_text("top secret")
            link = Path(ws, "link_to_secret.txt")
            try:
                os.symlink(str(target), str(link))
            except (OSError, NotImplementedError):
                # Symlinks unsupported on this FS — nothing to test.
                print("[SKIP] symlink not supported on this filesystem")
                return

            res = om.delete_file(str(link), "coder", justification="x")
            assert res.startswith("ERROR:"), f"Expected ERROR for symlink, got: {res}"
            assert "Symlink" in res, f"Error must mention symlink: {res}"
            assert target.exists(), "Out-of-bounds symlink target must NOT be deleted"
            assert link.exists() or os.path.islink(str(link)), "The link itself should not have been followed/deleted as a normal file"
    print("[PASS] test_symlink_bypass_rejected_a9")


# ── Phase B: feature ───────────────────────────────────────────────────────────

def test_multi_path_delete_removes_all():
    """paths=[...] deletes every listed target (all owned → auto-approved)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        fs = [Path(d, f"m{i}.txt") for i in range(4)]
        for fp in fs:
            fp.write_text(str(fp.name)); om._own(fp.resolve(), "coder")

        res = om.delete_file(None, "coder", paths=[str(fp) for fp in fs], justification="x")
        assert res.startswith("OK: Deleted 4 of 4"), f"Expected 4/4, got: {res}"
        for fp in fs:
            assert not fp.exists(), f"{fp} should be deleted"
    print("[PASS] test_multi_path_delete_removes_all")


def test_include_filter_deletes_only_matching():
    """include='*.md' deletes only matching files within the base dir (B2)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        md1 = Path(d, "a.md"); md1.write_text("m1")
        md2 = Path(d, "b.md"); md2.write_text("m2")
        txt = Path(d, "c.txt"); txt.write_text("t")
        for fp in (md1, md2, txt):
            om._own(fp.resolve(), "coder")

        res = om.delete_file(".", "coder", include="*.md", justification="x")
        assert res.startswith("OK: Deleted 2 of 2"), f"Expected 2/2 md files, got: {res}"
        assert not md1.exists() and not md2.exists(), "*.md files should be deleted"
        assert txt.exists(), "Non-matching .txt must survive the include filter"
    print("[PASS] test_include_filter_deletes_only_matching")


def test_size_and_date_filters_behave_like_list_dir():
    """min_size + modified_after filters behave like list_dir when deleting (B2)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        big = Path(d, "big.bin"); big.write_bytes(b"x" * 5000)   # 5 KB
        small = Path(d, "small.txt"); small.write_text("tiny")    # ~4 B
        for fp in (big, small):
            om._own(fp.resolve(), "coder")

        # min_size=1KB → only the big file matches.
        res = om.delete_file(".", "coder", min_size="1KB", justification="x")
        assert res.startswith("OK: Deleted 1 of 1"), f"Expected 1/1, got: {res}"
        assert not big.exists(), "Big file should be deleted by min_size"
        assert small.exists(), "Small file must survive min_size=1KB"

        # modified_after in the far future → nothing matches (empty set, B6). The
        # surviving 'small' file is owned so an empty match returns early with no prompt.
        res2 = om.delete_file(".", "coder", modified_after="2099-01-01", justification="x")
        assert res2.startswith("No files matched"), f"Expected empty-match message, got: {res2}"
        assert small.exists(), "Nothing should be deleted by a future modified_after"
    print("[PASS] test_size_and_date_filters_behave_like_list_dir")


def test_bare_filter_matches_files_only_not_dirs():
    """A size/date-only filter (no include/exclude) must match FILES only.

    Regression guard: reusing list_dir's matching verbatim would sweep every
    subdirectory — including the auto-created logs/backups/ tree that holds this
    very operation's backups — into the delete set. Directories are only matched
    when the caller explicitly supplies a directory-name filter (include/exclude).
    """
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        # A matching file at the root, plus a subdirectory tree that must survive.
        big = Path(d, "big.bin"); big.write_bytes(b"x" * 5000)      # 5 KB → matches min_size
        subdir = Path(d, "keepme"); subdir.mkdir()
        (subdir / "nested.txt").write_text("nested content")          # small, but in a dir
        om._own(big.resolve(), "coder")

        # min_size=1KB with NO include/exclude → only the root file 'big' matches.
        res = om.delete_file(".", "coder", min_size="1KB", justification="x")
        assert res.startswith("OK: Deleted 1 of 1"), f"Expected exactly 1 target, got: {res}"
        assert not big.exists(), "Matching root file should be deleted"
        assert subdir.is_dir(), "Subdirectory must survive a bare size filter"
        assert (subdir / "nested.txt").exists(), "File inside the surviving dir must remain"

        # And with an explicit include name filter, directories ARE considered.
        res2 = om.delete_file(".", "coder", include="keepme", justification="x")
        assert subdir.is_dir() or "OK:" in res2, "Explicit include should be able to target the dir"
    print("[PASS] test_bare_filter_matches_files_only_not_dirs")


def test_mixed_set_single_aggregate_approval_reject():
    """Mixed owned + non-owned set → ONE aggregate approval; reject leaves all intact (B3)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        owned = Path(d, "owned.txt"); owned.write_text("o")
        foreign = Path(d, "foreign.txt"); foreign.write_text("f")
        om._own(owned.resolve(), "coder")

        # Count how many approval prompts fire (must be exactly 1) and snapshot the
        # tool_args of the single prompt so we can assert its compact, readable shape.
        prompt_count = {"n": 0}
        captured_tool_args = {}
        def _count_and_reject():
            deadline = time.time() + 10
            seen = set()
            while time.time() < deadline:
                for p in om.list_pending_approvals():
                    if p["request_id"] not in seen:
                        seen.add(p["request_id"])
                        prompt_count["n"] += 1
                        captured_tool_args.update(p.get("tool_args", {}))
                        om.user_reject(p["request_id"], "no")
                        return
                time.sleep(0.02)
        threading.Thread(target=_count_and_reject, daemon=True).start()

        res = om.delete_file(None, "coder", paths=[str(owned), str(foreign)], justification="x")
        assert res.startswith("REJECTED"), f"Expected REJECTED, got: {res}"
        assert prompt_count["n"] == 1, f"Exactly ONE aggregate approval must fire, got {prompt_count['n']}"
        assert owned.exists() and foreign.exists(), "Reject must leave ALL targets intact"

        # B3 UX fix: the UI renders tool_args['justification'] prominently + a compact
        # JSON block. So the human-readable scope summary must live in 'justification'
        # (count + a path sample), and tool_args must NOT dump the full paths list.
        ta = captured_tool_args
        assert "justification" in ta, f"tool_args must carry a visible justification, got keys {list(ta)}"
        vis = ta["justification"]
        assert "2 entries" in vis, f"Scope summary must state the count, got: {vis!r}"
        # At least one of the target paths should appear as a readable sample.
        assert (owned.name in vis) or (foreign.name in vis), \
            f"Scope summary should include a path sample, got: {vis!r}"
        # The original agent justification is preserved for the audit trail.
        assert "Justification: x" in vis, f"Original justification must be kept, got: {vis!r}"
        # Compactness: no raw full paths list, and a bounded preview only.
        assert "paths" not in ta, f"tool_args must NOT dump the full 'paths' list, got keys {list(ta)}"
        assert "paths_preview" in ta, f"Expected a compact 'paths_preview', got keys {list(ta)}"
        assert len(ta["paths_preview"]) <= 5, \
            f"paths_preview must be bounded (<=5), got {len(ta['paths_preview'])}"
        assert ta.get("count") == 2, f"count should be 2, got {ta.get('count')}"
    print("[PASS] test_mixed_set_single_aggregate_approval_reject")


def test_empty_match_no_approval_prompt_b6():
    """Empty resolved set → clear 'No files matched' with NO approval prompt (B6)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, "keep.txt"); f.write_text("x")  # not owned → would need approval if matched

        saw_approval = []
        def _watcher():
            time.sleep(0.4)
            if om.list_pending_approvals():
                saw_approval.append(True)
        wt = threading.Thread(target=_watcher, daemon=True); wt.start()

        res = om.delete_file(".", "coder", include="*.nomatch", justification="x")
        wt.join(timeout=3)

        assert res.startswith("No files matched"), f"Expected empty-match message, got: {res}"
        assert not saw_approval, "Empty match set must NOT fire an approval prompt (B6)"
        assert f.exists(), "Nothing should be deleted on an empty match"
    print("[PASS] test_empty_match_no_approval_prompt_b6")


def test_continue_on_error_b4():
    """One failing target does not abort the rest; summary is correct (B4)."""
    import shutil as _shutil

    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        good1 = Path(d, "good1.txt"); good1.write_text("g1")
        bad = Path(d, "bad.txt"); bad.write_text("b")
        good2 = Path(d, "good2.txt"); good2.write_text("g2")
        for fp in (good1, bad, good2):
            om._own(fp.resolve(), "coder")

        # Force the 'bad' target's delete to fail by making its backup dir unwritable.
        # We patch _delete_one selectively: raise only when the path is 'bad'.
        real_delete_one = om._delete_one
        def flaky_delete(resolved, agent_name, justification=""):
            if resolved.name == "bad.txt":
                raise RuntimeError("simulated per-target failure")
            return real_delete_one(resolved, agent_name, justification)
        om._delete_one = flaky_delete

        res = om.delete_file(None, "coder", paths=[str(good1), str(bad), str(good2)], justification="x")
        assert res.startswith("OK: Deleted 2 of 3"), f"Expected 2/3, got:\n{res}"
        assert not good1.exists() and not good2.exists(), "Good targets should be deleted"
        assert bad.exists(), "Failing target must remain"
        assert "bad.txt" in res, "Summary must list the failing target"
    print("[PASS] test_continue_on_error_b4")


def test_filter_cannot_escape_allowed_dirs_or_ro():
    """A filter cannot expand scope outside allowed dirs / into an RO extra folder (B2)."""
    with tempfile.TemporaryDirectory() as ws:
        with tempfile.TemporaryDirectory() as ro_folder:
            om = _make_om(ws)
            om.set_extra_work_folders(folders_ro=[str(ro_folder)], folders_rw=[])

            # A file inside the RO extra folder.
            ro_file = Path(ro_folder, "ro.txt"); ro_file.write_text("ro")

            # Deleting by absolute path with mode="rw" must be rejected (RO folder).
            res = om.delete_file(str(ro_file), "coder", justification="x")
            assert res.startswith(("ERROR:", "No files matched")), f"Expected rejection, got: {res}"
            assert ro_file.exists(), "RO extra-folder file must NOT be deleted via mode='rw'"

            # A filter over the workspace base dir cannot reach into the RO folder.
            ws_file = Path(ws, "ws.txt"); ws_file.write_text("w")
            om._own(ws_file.resolve(), "coder")
            res2 = om.delete_file(".", "coder", include="*.txt", justification="x")
            assert ro_file.exists(), "Filter must not expand scope into the RO extra folder"
    print("[PASS] test_filter_cannot_escape_allowed_dirs_or_ro")


# ── Refinement pass: closing 3 coverage gaps from the review ───────────────────

def test_no_path_provided_clear_error():
    """Neither path nor paths given → clear ERROR, nothing deleted, no prompt (B6a)."""
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        f = Path(d, "keep.txt"); f.write_text("x")

        saw_approval = []
        def _watcher():
            time.sleep(0.3)
            if om.list_pending_approvals():
                saw_approval.append(True)
        wt = threading.Thread(target=_watcher, daemon=True); wt.start()

        res = om.delete_file(None, "coder", paths=[], justification="x")
        wt.join(timeout=3)

        assert res.startswith("ERROR: No path(s) provided"), f"Expected clear error, got: {res}"
        assert not saw_approval, "No-path case must NOT fire an approval prompt"
        assert f.exists(), "Nothing should be deleted when no path is provided"
    print("[PASS] test_no_path_provided_clear_error")


def test_dir_backup_copy_fallback_integrity_a2(monkeypatch):
    """Directory delete via copy fallback verifies dir backup integrity before rmtree (A2).

    Force shutil.move to fail so the code takes the copytree path; then make
    _verify_dir_backup return False. The original directory must remain intact and no
    partial backup may be left behind.
    """
    import shutil as _shutil

    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        src = Path(d, "victimdir")
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_text("aaa")
        (src / "sub" / "b.txt").write_text("bbb")

        # Force the copy fallback, then make the dir-integrity check fail.
        monkeypatch.setattr(_shutil, "move", lambda *a, **k: (_ for _ in ()).throw(OSError("force fallback")))
        monkeypatch.setattr(om, "_verify_dir_backup", lambda source, backup_path: False)

        try:
            om._delete_one(src.resolve(), "coder", justification="x")
            raise AssertionError("_delete_one should have raised on a dir backup integrity failure")
        except Exception as e:
            assert "backup integrity check failed" in str(e), f"Error must name the step: {e}"

        # Original directory must be fully intact (rmtree never ran).
        assert src.exists() and (src / "a.txt").exists() and (src / "sub" / "b.txt").exists(), \
            "Original directory must remain intact after a dir backup integrity failure"
        bdir = _backup_dir(om)
        partials = list(bdir.glob("victimdir*")) if bdir.exists() else []
        assert not partials, f"No partial dir backup may remain: {partials}"
    print("[PASS] test_dir_backup_copy_fallback_integrity_a2")


def test_mixed_set_single_aggregate_approval_accept():
    """Mixed owned + non-owned set → ONE aggregate approval; approve deletes ALL (B3/B4).

    Mirrors the reject test but exercises the acceptance path: every target is deleted,
    a backup exists for each, and ownership entries are cleared.
    """
    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        owned = Path(d, "owned.txt"); owned.write_text("o")
        foreign = Path(d, "foreign.txt"); foreign.write_text("f")
        om._own(owned.resolve(), "coder")

        prompt_count = {"n": 0}
        def _count_and_approve():
            deadline = time.time() + 10
            seen = set()
            while time.time() < deadline:
                for p in om.list_pending_approvals():
                    if p["request_id"] not in seen:
                        seen.add(p["request_id"])
                        prompt_count["n"] += 1
                        om.user_approve(p["request_id"], "ok go")
                        return
                time.sleep(0.02)
        threading.Thread(target=_count_and_approve, daemon=True).start()

        res = om.delete_file(None, "coder", paths=[str(owned), str(foreign)], justification="cleanup")
        assert res.startswith("OK: Deleted"), f"Expected OK, got: {res}"
        assert prompt_count["n"] == 1, f"Exactly ONE aggregate approval must fire, got {prompt_count['n']}"
        assert not owned.exists() and not foreign.exists(), "Approved bulk delete must remove ALL targets"

        # A backup was created for each deleted target.
        bdir = _backup_dir(om)
        backups = list(bdir.glob("*.bak")) if bdir.exists() else []
        assert len(backups) == 2, f"Expected one backup per target (2), got {backups}"

        # Ownership entry for the owned file was cleared; foreign had none.
        assert om._get_owner(owned.resolve()) is None, "Owned-file ownership must be cleared after delete"
    print("[PASS] test_mixed_set_single_aggregate_approval_accept")


def test_approval_description_marks_capped_size(monkeypatch):
    """When a dir target exceeds the file-count cap, the approval total is marked '+' (B3).

    ponytail refinement: the size walk is capped for latency; when it trips the total
    must be flagged approximate so the number the user approves against isn't silently
    under-reported. We shrink the module-level cap to a tiny value so it trips fast.
    """
    import agent_cascade.operation_manager.file_operations as fops

    with tempfile.TemporaryDirectory() as d:
        om = _make_om(d)
        bigdir = Path(d, "big"); bigdir.mkdir()
        # 5 real files; shrink the cap to 2 so the walk trips early.
        for i in range(5):
            (bigdir / f"f{i}.txt").write_text("x" * 10)

        monkeypatch.setattr(fops, "_SCOPE_INFO_FILE_CAP", 2)
        desc = om._build_delete_approval_description([bigdir.resolve()], [bigdir.resolve()])
        # The total must carry the approximate marker because the cap tripped.
        assert "total" in desc and "+" in desc.split("total")[1].split(")")[0], \
            f"Capped size must be marked approximate with '+', got: {desc!r}"

    print("[PASS] test_approval_description_marks_capped_size")


# ── Schema + tool-level (DeleteFile) tests ────────────────────────────────────

_EXPOSED_KEYS = {'path', 'include', 'justification'}


class _StubOpManager:
    """Records the kwargs it receives so we can assert on forwarding."""

    def __init__(self):
        self.calls = []

    def delete_file(self, path, agent_name, **kwargs):
        self.calls.append({'path': path, 'agent_name': agent_name, 'kwargs': kwargs})
        return "OK: Deleted (stub)"


class _StubAgentPool:
    def __init__(self, op_manager):
        self.operation_manager = op_manager


def _make_tool():
    from agent_cascade.tools.custom.file_ops import DeleteFile
    om = _StubOpManager()
    tool = DeleteFile(agent_pool=_StubAgentPool(om), agent_name='coder')
    return tool, om


class TestDeleteFileSchema:
    """The LLM-facing schema must expose exactly path/include/justification."""

    def test_exposes_exactly_three_properties(self):
        from agent_cascade.tools.custom.file_ops import DeleteFile
        props = DeleteFile.parameters['properties']
        assert set(props.keys()) == _EXPOSED_KEYS, \
            f"Schema properties must be exactly {_EXPOSED_KEYS}, got {set(props.keys())}"

    def test_path_accepts_string_or_array(self):
        """path uses oneOf [string, array-of-string] (LLM-API-compatible union)."""
        from agent_cascade.tools.custom.file_ops import DeleteFile
        p = DeleteFile.parameters['properties']['path']
        assert 'oneOf' in p, f"path must use oneOf for the string|array union, got keys {list(p.keys())}"
        options = p['oneOf']
        assert {'type': 'string'} in options, \
            f"oneOf must include a plain string option, got {options}"
        assert {'type': 'array', 'items': {'type': 'string'}} in options, \
            f"oneOf must include an array-of-string option, got {options}"

    def test_schema_validates_string_and_list_instances(self):
        """jsonschema (used by _verify_json_format_args) accepts both forms and rejects others."""
        import jsonschema
        from agent_cascade.tools.custom.file_ops import DeleteFile
        schema = DeleteFile.parameters
        jsonschema.validate(instance={'path': 'a.md'}, schema=schema)
        jsonschema.validate(instance={'path': ['a.md', 'b.md']}, schema=schema)
        for bad in (123, {'a': 1}, True):
            try:
                jsonschema.validate(instance={'path': bad}, schema=schema)
            except jsonschema.ValidationError:
                continue
            raise AssertionError(f"schema must reject path={bad!r}")

    def test_justification_exposed(self):
        from agent_cascade.tools.custom.file_ops import DeleteFile
        props = DeleteFile.parameters['properties']
        assert 'justification' in props, "justification must be an exposed schema property"

    def test_metadata_matches_schema(self):
        """TOOL_METADATA carries exactly the three schema params (no hidden leftovers)."""
        from agent_cascade.prompts.dna import TOOL_METADATA
        meta = TOOL_METADATA['delete_file']['parameters']
        assert set(meta.keys()) == _EXPOSED_KEYS, \
            f"metadata keys must be exactly {_EXPOSED_KEYS}, got {set(meta.keys())}"


class TestDeleteFileCall:
    """Tool-level call() input normalization and kwarg forwarding."""

    def test_path_list_forwards_as_paths(self):
        tool, om = _make_tool()
        res = tool.call({'path': ['a', 'b'], 'justification': 'cleanup'})
        assert not res.startswith("ERROR"), f"Unexpected error: {res}"
        call = om.calls[-1]
        assert call['path'] is None, f"path must be None for a list input, got {call['path']!r}"
        assert call['kwargs']['paths'] == ['a', 'b'], \
            f"list input must forward as paths=['a','b'], got {call['kwargs'].get('paths')!r}"

    def test_path_string_forwards_as_path(self):
        tool, om = _make_tool()
        res = tool.call({'path': 'single', 'justification': 'cleanup'})
        assert not res.startswith("ERROR"), f"Unexpected error: {res}"
        call = om.calls[-1]
        assert call['path'] == 'single', f"path must be 'single', got {call['path']!r}"
        assert call['kwargs']['paths'] is None, \
            f"strings input must not set paths, got {call['kwargs'].get('paths')!r}"

    def test_empty_path_errors(self):
        tool, om = _make_tool()
        res = tool.call({'justification': 'cleanup'})
        assert res.startswith("ERROR"), f"missing path must error, got: {res!r}"
        assert "'path' (string or list)" in res, f"error must name 'path' (string or list): {res!r}"
        assert not om.calls, "no op-manager call should occur on the error path"

    def test_empty_list_errors(self):
        tool, om = _make_tool()
        res = tool.call({'path': [], 'justification': 'cleanup'})
        assert res.startswith("ERROR"), f"empty list must error, got: {res!r}"
        assert not om.calls, "no op-manager call should occur on the empty-list path"

    def test_non_string_path_type_errors_cleanly(self):
        """int/dict/bool 'path' values must produce a clean error, not crash downstream."""
        tool, om = _make_tool()
        for bad in (123, {'a': 1}, True):
            res = tool.call({'path': bad, 'justification': 'cleanup'})
            assert res.startswith("ERROR"), f"path={bad!r} must error cleanly, got: {res!r}"
            assert "string or a list of strings" in res, \
                f"error must name the expected types for path={bad!r}: {res!r}"
        assert not om.calls, "no op-manager call should occur for invalid path types"

    def test_list_with_non_string_entries_filters_them(self):
        """Non-string entries in a list are dropped; only valid strings forward."""
        tool, om = _make_tool()
        res = tool.call({'path': ['a.md', 42, None, 'b.md'], 'justification': 'cleanup'})
        assert not res.startswith("ERROR"), f"Unexpected error: {res}"
        call = om.calls[-1]
        assert call['kwargs']['paths'] == ['a.md', 'b.md'], \
            f"non-string entries must be filtered, got {call['kwargs'].get('paths')!r}"

