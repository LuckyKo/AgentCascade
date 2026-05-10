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

import copy
import datetime
import io
import os
import pickle
import traceback
from concurrent.futures import TimeoutError
from contextlib import redirect_stdout
from functools import partial
from typing import Any, Dict, List, Optional, Union

import json5
import regex
from tqdm import tqdm

from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.utils.utils import extract_code


class GenericRuntime:
    GLOBAL_DICT = {}
    LOCAL_DICT = None
    HEADERS = []

    def __init__(self):
        self._global_vars = copy.copy(self.GLOBAL_DICT)
        self._local_vars = copy.copy(self.LOCAL_DICT) if self.LOCAL_DICT else None

        for c in self.HEADERS:
            self.exec_code(c)

    def exec_code(self, code_piece: str) -> None:
        if regex.search(r'(\s|^)?input\(', code_piece) or regex.search(r'(\s|^)?os.system\(', code_piece):
            raise RuntimeError()
        exec(code_piece, self._global_vars)

    def eval_code(self, expr: str) -> Any:
        return eval(expr, self._global_vars)

    def inject(self, var_dict: Dict[str, Any]) -> None:
        for k, v in var_dict.items():
            self._global_vars[k] = v

    @property
    def answer(self):
        return self._global_vars['answer']


class DateRuntime(GenericRuntime):
    import dateutil.relativedelta
    GLOBAL_DICT = {
        'datetime': datetime.datetime,
        'timedelta': dateutil.relativedelta.relativedelta,
        'relativedelta': dateutil.relativedelta.relativedelta
    }


class CustomDict(dict):

    def __iter__(self):
        return list(super().__iter__()).__iter__()


class ColorObjectRuntime(GenericRuntime):
    GLOBAL_DICT = {'dict': CustomDict}


def _check_deps_for_python_executor():
    try:
        import dateutil.relativedelta  # noqa
        import multiprocess  # noqa
        from multiprocess import Pool  # noqa
        from pebble import ProcessPool  # noqa
        from timeout_decorator import timeout  # noqa
    except ImportError as e:
        raise ImportError(
            'The dependencies for Python Executor support are not installed. '
            'Please install the required dependencies by running: pip install "agent-cascade[python_executor]"') from e


# @register_tool('python_executor')
class PythonExecutor(BaseTool):
    name = 'python_executor'
    description = 'Executes Python code DIRECTLY on the host machine (NOT sandboxed). Use this ONLY when you need to access host-side services on 127.0.0.1 that Docker cannot reach. Highly dangerous, use with caution.'
    parameters = {
        'type': 'object',
        'properties': {
            'code': {
                'description': 'The python code.',
                'type': 'string',
            }
        },
        'required': ['code'],
    }

    def __init__(self, cfg: Optional[Dict] = None):
        _check_deps_for_python_executor()
        import multiprocess
        from multiprocess import Pool
        super().__init__(cfg)

        runtime: Optional[Any] = self.cfg.get('runtime', None)
        get_answer_symbol: Optional[str] = self.cfg.get('get_answer_symbol', None)
        get_answer_expr: Optional[str] = self.cfg.get('get_answer_expr', None)
        get_answer_from_stdout: bool = self.cfg.get('get_answer_from_stdout', True)
        timeout_length: int = self.cfg.get('timeout_length', 20)
        self.work_dir: str = self.cfg.get('work_dir', '')

        self.runtime = runtime if runtime else GenericRuntime()
        self.answer_symbol = get_answer_symbol
        self.answer_expr = get_answer_expr
        self.get_answer_from_stdout = get_answer_from_stdout
        self.pool = Pool(multiprocess.cpu_count())
        self.timeout_length = timeout_length

    def call(self, params: Union[str, dict], **kwargs) -> list:
        try:
            params = self._verify_json_format_args(params)
            code = params.get('code', '')
        except Exception:
            if isinstance(params, dict):
                params_str = json.dumps(params)
            else:
                params_str = params
            code = extract_code(params_str)

        if not str(code).strip():
            return ['', '']

        predictions = self.apply(code)
        return predictions

    def apply(self, code: str) -> list:
        return self.batch_apply([code])[0]

    def process_generation_to_code(self, gens: str):
        return [g.split('\n') for g in gens]

    @staticmethod
    def execute(
        code,
        get_answer_from_stdout=None,
        runtime=None,
        answer_symbol=None,
        answer_expr=None,
        timeout_length=20,
        work_dir='',
    ):
        from timeout_decorator import timeout
        import signal
        use_timeout = hasattr(signal, 'SIGALRM')
        old_cwd = os.getcwd()
        
        result = ''
        report = 'Done'
        try:
            if work_dir:
                os.makedirs(work_dir, exist_ok=True)
                os.chdir(work_dir)

            if get_answer_from_stdout:
                program_io = io.StringIO()
                with redirect_stdout(program_io):
                    if use_timeout:
                        timeout(timeout_length)(runtime.exec_code)('\n'.join(code))
                    else:
                        runtime.exec_code('\n'.join(code))
                program_io.seek(0)
                result = program_io.read()
            elif answer_symbol:
                if use_timeout:
                    timeout(timeout_length)(runtime.exec_code)('\n'.join(code))
                else:
                    runtime.exec_code('\n'.join(code))
                result = runtime._global_vars[answer_symbol]
            elif answer_expr:
                if use_timeout:
                    timeout(timeout_length)(runtime.exec_code)('\n'.join(code))
                    result = timeout(timeout_length)(runtime.eval_code)(answer_expr)
                else:
                    runtime.exec_code('\n'.join(code))
                    result = runtime.eval_code(answer_expr)
            else:
                if use_timeout:
                    timeout(timeout_length)(runtime.exec_code)('\n'.join(code[:-1]))
                    result = timeout(timeout_length)(runtime.eval_code)(code[-1])
                else:
                    runtime.exec_code('\n'.join(code[:-1]))
                    result = runtime.eval_code(code[-1])
            str(result)
            pickle.dumps(result)  # serialization check
        except Exception:
            report = traceback.format_exc().split('\n')[-2]
        finally:
            os.chdir(old_cwd)
        return result, report

    @staticmethod
    def truncate(s, max_length=256):
        half = max_length // 2
        if len(s) > max_length:
            s = s[:half] + '...' + s[-half:]
        return s

    def batch_apply(self, batch_code: List[str]) -> list:
        from pebble import ProcessPool
        all_code_snippets = self.process_generation_to_code(batch_code)

        timeout_cnt = 0
        all_exec_results = []
        with ProcessPool(max_workers=min(len(all_code_snippets), os.cpu_count())) as pool:
            executor = partial(
                self.execute,
                get_answer_from_stdout=self.get_answer_from_stdout,
                runtime=self.runtime,
                answer_symbol=self.answer_symbol,
                answer_expr=self.answer_expr,
                timeout_length=self.timeout_length,  # this timeout not work
                work_dir=self.work_dir,
            )
            future = pool.map(executor, all_code_snippets, timeout=self.timeout_length)
            iterator = future.result()

            if len(all_code_snippets) > 100:
                progress_bar = tqdm(total=len(all_code_snippets), desc='Execute')
            else:
                progress_bar = None

            while True:
                try:
                    result = next(iterator)
                    all_exec_results.append(result)
                except StopIteration:
                    break
                except TimeoutError as error:
                    print(error)
                    all_exec_results.append(('', 'Timeout Error'))
                    timeout_cnt += 1
                except Exception as error:
                    print(error)
                    all_exec_results.append(('', str(error)))
                if progress_bar is not None:
                    progress_bar.update(1)

            if progress_bar is not None:
                progress_bar.close()

        batch_results = []
        for code, (res, report) in zip(all_code_snippets, all_exec_results):
            # post processing
            res, report = str(res).strip(), str(report).strip()
            res, report = self.truncate(res), self.truncate(report)
            batch_results.append((res, report))
        return batch_results


def _test():
    batch_code = ["""
        print("Hello world!")
        """]

    executor = PythonExecutor(get_answer_from_stdout=True)
    predictions = executor.apply(batch_code[0])
    print(predictions)


if __name__ == '__main__':
    _test()
