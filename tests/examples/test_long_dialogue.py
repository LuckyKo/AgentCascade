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
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(__file__, '../../..')))  # noqa

ROOT_RESOURCE = os.path.abspath(os.path.join(__file__, '../../../examples/resource'))  # noqa
from examples.long_dialogue import test as long_dialogue  # noqa


_HAS_DASHSCOPE_KEY = bool(os.getenv('DASHSCOPE_API_KEY', '').strip())


@pytest.mark.extra_examples
@pytest.mark.skipif(not _HAS_DASHSCOPE_KEY, reason="Requires DASHSCOPE_API_KEY (live external API)")
def test_long_dialogue():
    """Test long dialogue with DashScope qwen-max — requires external API."""
    long_dialogue()
