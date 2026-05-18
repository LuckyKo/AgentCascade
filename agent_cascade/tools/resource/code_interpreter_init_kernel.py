# Copyright 2023 The Qwen team, Alibaba Group. All rights reserved.
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#    http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json  # noqa
import math  # noqa
import os  # noqa
import re  # noqa
import signal
import threading

import matplotlib  # noqa
import matplotlib.pyplot as plt
import numpy as np  # noqa
import pandas as pd  # noqa
import seaborn as sns
from matplotlib.font_manager import FontProperties
from sympy import Eq, solve, symbols  # noqa


def input(*args, **kwargs):  # noqa
    raise NotImplementedError('Python input() function is disabled.')


def _m6_timout_handler(_signum=None, _frame=None):
    raise TimeoutError('M6_CODE_INTERPRETER_TIMEOUT')


# Unix timer using signal.alarm (works on Linux/macOS)
_unix_timer_available = False
try:
    signal.signal(signal.SIGALRM, _m6_timout_handler)
    _unix_timer_available = True
except AttributeError:  # windows
    pass

# Windows fallback: flag-based threading timer
# On Windows, signal.alarm is not available. We use a daemon thread that sets
# a flag on __main__ when the timeout expires. The main execution loop checks
# this flag periodically and raises TimeoutError if set.
# NOTE: This kernel-side timer sets a flag but there's no periodic check in user code
# to actually observe it — the flag is a best-effort early signal. The real timeout
# protection comes from Layer 1: the parent process calls get_iopub_msg(timeout=timeout)
# which detects stalled kernels and interrupts them. This still prevents system hangs,
# just at the cost of waiting the full timeout duration before recovery.
_windows_timer = None  # Store the Timer object so we can cancel it

def _windows_timeout_worker(timeout: int):
    """Worker thread for Windows timeout fallback — sets a flag instead of raising."""
    global _windows_timer
    import sys
    main_mod = sys.modules.get('__main__')
    if main_mod is not None:
        # Timeout expired without being cancelled — set the flag
        main_mod._M6_TIMEOUT_FLAG = True


class _M6CountdownTimer:

    @classmethod
    def start(cls, timeout: int):
        global _windows_timer
        if _unix_timer_available:
            try:
                signal.alarm(timeout)
            except AttributeError:
                pass
        else:
            # Windows fallback: use threading.Timer with flag-based approach
            import sys
            main_mod = sys.modules.get('__main__')
            if main_mod is not None:
                main_mod._M6_TIMEOUT_FLAG = False  # Reset flag before starting
            
            # Cancel any existing timer before starting a new one
            if _windows_timer is not None and _windows_timer.is_alive():
                _windows_timer.cancel()
            
            _windows_timer = threading.Timer(timeout, _windows_timeout_worker, args=[timeout])
            _windows_timer.daemon = True
            _windows_timer.start()

    @classmethod
    def cancel(cls):
        global _windows_timer
        if _unix_timer_available:
            try:
                signal.alarm(0)
            except AttributeError:
                pass
        else:
            # Cancel the timer and reset the flag
            import sys
            main_mod = sys.modules.get('__main__')
            if main_mod is not None:
                main_mod._M6_TIMEOUT_FLAG = False
            
            if _windows_timer is not None and _windows_timer.is_alive():
                _windows_timer.cancel()


sns.set_theme()

_m6_font_prop = FontProperties(fname='{{M6_FONT_PATH}}')
plt.rcParams['font.family'] = _m6_font_prop.get_name()
