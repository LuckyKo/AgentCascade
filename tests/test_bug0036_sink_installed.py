"""BUG_0036 follow-up — prove the EBADF-tolerant sink is ACTUALLY installed.

The original implementation called install_tolerant_terminal_sink at pytest_configure,
but the terminal reporter does not exist yet at that hook (verified by live probe:
tr=N/tw=N at configure, tr=Y/tw=Y with a real file at sessionstart — in both controller
and workers). So the defensive guard always skipped and the proxy was never applied.

The fix moves the install to pytest_sessionstart. This test proves the proxy is now
present on the live terminal reporter's writer sink. Run it under -n 2 so it executes
in the xdist workers (where BUG_0036 actually crashes) — confirming installation in
each worker process. (The controller does not run tests by default, so this asserts
the worker path; run without -n to exercise the controller path.)
"""

import os


def test_tolerant_sink_installed_on_live_reporter(request):
    """The terminalreporter's _tw._file must be a _TolerantFileProxy at test time.

    At test-execution time (well after sessionstart) the sink must already be wrapped,
    in whichever process this runs (controller or xdist worker).
    """
    from tests._ebadf_sink import _TolerantFileProxy

    tr = request.config.pluginmanager.get_plugin('terminalreporter')
    assert tr is not None, 'terminalreporter plugin missing'
    tw = getattr(tr, '_tw', None)
    assert tw is not None, 'terminalreporter._tw missing'
    sink = getattr(tw, '_file', None)
    worker = os.environ.get('PYTEST_XDIST_WORKER', '<controller>')

    # This assertion is the whole point: the proxy must be installed in THIS process.
    assert isinstance(sink, _TolerantFileProxy), (
        f"BUG_0036 sink NOT installed in {worker}: "
        f"_tw._file is {type(sink).__name__}, expected _TolerantFileProxy"
    )
