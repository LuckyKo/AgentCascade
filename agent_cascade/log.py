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

import logging
import os
from pathlib import Path


def setup_logger(level=None):
    if level is None:
        if os.getenv('QWEN_AGENT_DEBUG', '0').strip().lower() in ('1', 'true'):
            level = logging.DEBUG
        else:
            level = logging.INFO

    handler = logging.StreamHandler()
    # Do not run handler.setLevel(level) so that users can change the level via logger.setLevel later
    formatter = logging.Formatter('%(asctime)s - %(filename)s - %(lineno)d - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)

    _logger = logging.getLogger('agent_cascade_logger')
    _logger.setLevel(level)
    
    # Only add handlers once (prevent duplicates on restart/reload)
    if not _logger.handlers:
        _logger.addHandler(handler)

        # File handler — console log to logs/console.log (RotatingFileHandler with max 10MB per file, 5 backups)
        log_dir = Path(__file__).resolve().parent.parent / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            log_dir / 'console.log', 
            maxBytes=10 * 1024 * 1024,  # 10MB per file
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        _logger.addHandler(file_handler)

    return _logger


logger = setup_logger()
