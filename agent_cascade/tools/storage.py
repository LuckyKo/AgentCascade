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

import os
from typing import Dict, Optional, Union

from agent_cascade.settings import DEFAULT_WORKSPACE
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.utils.utils import read_text_from_file, save_text_to_file


class KeyNotExistsError(ValueError):
    pass


@register_tool('storage')
class Storage(BaseTool):
    """
    A tool for persistent data storage and retrieval. 
    Allows agents to save, read, delete, and scan information using key-value pairs.
    """
    description = 'A tool for storing and retrieving data.'
    parameters = {
        'type': 'object',
        'properties': {
            'operate': {
                'description': 'The type of data operation: "put" (save data), "get" (read data), "delete" (remove data), or "scan" (list/read multiple items).',
                'type': 'string',
            },
            'key': {
                'description': 'The unique identifier (path-like) for the data. Use "/" as the default root. Design clear and unique paths (e.g., "/notes/summary").',
                'type': 'string',
                'default': '/'
            },
            'value': {
                'description': 'The content to be stored. Required only for the "put" operation.',
                'type': 'string',
            },
        },
        'required': ['operate'],
    }

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        self.root = self.cfg.get('storage_root_path', os.path.join(DEFAULT_WORKSPACE, 'tools', self.name))
        os.makedirs(self.root, exist_ok=True)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        params = self._verify_json_format_args(params)
        operate = params['operate']
        key = params.get('key', '/')
        if key.startswith('/'):
            key = key[1:]

        if operate == 'put':
            assert 'value' in params
            return self.put(key, params['value'])
        elif operate == 'get':
            return self.get(key)
        elif operate == 'delete':
            return self.delete(key)
        else:
            return self.scan(key)

    def put(self, key: str, value: str, path: Optional[str] = None) -> str:
        path = path or self.root

        # one file for one key value pair
        path = os.path.join(path, key)

        path_dir = path[:path.rfind('/') + 1]
        if path_dir:
            os.makedirs(path_dir, exist_ok=True)

        save_text_to_file(path, value)
        return f'Successfully saved content to key: {key}'

    def get(self, key: str, path: Optional[str] = None) -> str:
        path = path or self.root
        if not os.path.exists(os.path.join(path, key)):
            raise KeyNotExistsError(f'Get Failed: {key} does not exist')
        return read_text_from_file(os.path.join(path, key))

    def delete(self, key, path: Optional[str] = None) -> str:
        path = path or self.root
        path = os.path.join(path, key)
        if os.path.exists(path):
            os.remove(path)
            return f'Successfully deleted {key}'
        else:
            return f'Delete Failed: {key} does not exist'

    def scan(self, key: str, path: Optional[str] = None) -> str:
        path = path or self.root
        path = os.path.join(path, key)
        if os.path.exists(path):
            if not os.path.isdir(path):
                return 'Scan Failed: The scan operation requires a folder path (directory key).'
            # All key-value pairs
            kvs = {}
            for root, dirs, files in os.walk(path):
                for file in files:
                    k = os.path.join(root, file)[len(path):]
                    if not k.startswith('/'):
                        k = '/' + k
                    v = read_text_from_file(os.path.join(root, file))
                    kvs[k] = v
            return '\n'.join([f'{k}: {v}' for k, v in kvs.items()])
        else:
            return f'Scan Failed: {key} does not exist.'
