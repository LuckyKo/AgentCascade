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

import json
import os
from typing import Dict, Optional, Union

import json5

from agent_cascade.settings import DEFAULT_WORKSPACE
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.tools.search_tools.keyword_search import WORDS_TO_IGNORE, string_tokenizer
from agent_cascade.tools.simple_doc_parser import SimpleDocParser
from agent_cascade.tools.storage import KeyNotExistsError, Storage


@register_tool('extract_doc_vocabulary')
class ExtractDocVocabulary(BaseTool):
    description = 'Extract the vocabulary of the document.'
    parameters = {
        'type': 'object',
        'properties': {
            'files': {
                'description': 'A list of file paths, supporting local file paths or downloadable http(s) links.',
                'type': 'array',
                'items': {
                    'type': 'string'
                },
            }
        },
        'required': ['files'],
    }

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        self.work_dir: str = self.cfg.get('work_dir', '')
        self.simple_doc_parse = SimpleDocParser(cfg={'work_dir': self.work_dir})

        self.data_root = self.cfg.get('path', os.path.join(DEFAULT_WORKSPACE, 'tools', self.name))
        self.db = Storage({'storage_root_path': self.data_root})

    def call(self, params: Union[str, dict], **kwargs) -> str:
        params = self._verify_json_format_args(params)
        files = params.get('files', [])
        document_id = str(files)

        if isinstance(files, str):
            files = json5.loads(files)
        docs = []
        for file in files:
            _doc = self.simple_doc_parse.call(params={'url': file}, **kwargs)
            docs.append(_doc)

        try:
            all_voc = self.db.call({'operate': 'get', 'key': document_id})
        except KeyNotExistsError:
            try:
                from sklearn.feature_extraction.text import TfidfVectorizer
            except ModuleNotFoundError:
                raise ModuleNotFoundError('Please install sklearn by: `pip install scikit-learn`')

            vectorizer = TfidfVectorizer(tokenizer=string_tokenizer, stop_words=WORDS_TO_IGNORE)
            tfidf_matrix = vectorizer.fit_transform(docs)
            sorted_items = sorted(zip(vectorizer.get_feature_names_out(),
                                      tfidf_matrix.toarray().flatten()),
                                  key=lambda x: x[1],
                                  reverse=True)
            all_voc = ', '.join([term for term, score in sorted_items])
            if document_id:
                self.db.call({'operate': 'put', 'key': document_id, 'value': json.dumps(all_voc, ensure_ascii=False)})

        return all_voc
