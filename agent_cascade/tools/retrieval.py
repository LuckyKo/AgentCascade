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

from typing import Dict, Optional, Union

import json5

from agent_cascade.settings import DEFAULT_MAX_REF_TOKEN, DEFAULT_PARSER_PAGE_SIZE, DEFAULT_RAG_SEARCHERS
from agent_cascade.tools.base import TOOL_REGISTRY, BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.tools.doc_parser import DocParser, Record
from agent_cascade.tools.simple_doc_parser import PARSER_SUPPORTED_FILE_TYPES


def _check_deps_for_rag():
    try:
        import charset_normalizer  # noqa
        import jieba  # noqa
        import pdfminer  # noqa
        import pdfplumber  # noqa
        import rank_bm25  # noqa
        import snowballstemmer  # noqa
        from bs4 import BeautifulSoup  # noqa
        from docx import Document  # noqa
        from pptx import Presentation  # noqa
    except ImportError as e:
        raise ImportError('The dependencies for RAG support are not installed. '
                          'Please install the required dependencies by running: pip install "agent-cascade[rag]"') from e


@register_tool('retrieval')
class Retrieval(BaseTool):
    description = TOOL_METADATA['retrieval']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'query': {
                'description': TOOL_METADATA['retrieval']['parameters']['query'],
                'type': 'string',
            },
            'files': {
                'description': TOOL_METADATA['retrieval']['parameters']['files'],
                'type': 'array',
                'items': {
                    'type': 'string'
                }
            },
        },
        'required': ['query', 'files'],
    }

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        self.max_ref_token: int = self.cfg.get('max_ref_token', DEFAULT_MAX_REF_TOKEN)
        self.parser_page_size: int = self.cfg.get('parser_page_size', DEFAULT_PARSER_PAGE_SIZE)
        self.work_dir: str = self.cfg.get('work_dir', '')
        self.doc_parse = DocParser({
            'max_ref_token': self.max_ref_token,
            'parser_page_size': self.parser_page_size,
            'work_dir': self.work_dir
        })

        self.rag_searchers = self.cfg.get('rag_searchers', DEFAULT_RAG_SEARCHERS)
        if len(self.rag_searchers) == 1:
            self.search = TOOL_REGISTRY[self.rag_searchers[0]]({'max_ref_token': self.max_ref_token})
        else:
            from agent_cascade.tools.search_tools.hybrid_search import HybridSearch
            self.search = HybridSearch({'max_ref_token': self.max_ref_token, 'rag_searchers': self.rag_searchers})

    def call(self, params: Union[str, dict], **kwargs) -> list:
        """RAG tool.

        Step1: Parse and save files
        Step2: Retrieval related content according to query

        Args:
            params: The files and query.
        Returns:
            The parsed file list or retrieved file list.
        """

        # TODO: It this a good place to check the RAG deps?
        _check_deps_for_rag()

        params = self._verify_json_format_args(params)
        files = params.get('files', [])
        if isinstance(files, str):
            files = json5.loads(files)
        records = []
        for file in files:
            _record = self.doc_parse.call(params={'url': file}, **kwargs)
            records.append(_record)

        query = params.get('query', '')
        if records:
            return self.search.call(params={'query': query}, docs=[Record(**rec) for rec in records], **kwargs)
        else:
            return []
