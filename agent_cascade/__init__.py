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

__version__ = '0.0.34'

# Lazy imports to avoid circular dependency deadlocks when importing submodules
# (e.g., 'from agent_cascade.instance_id import get_instance_id').
# The heavy Agent/MultiAgentHub imports are deferred until first access.
def __getattr__(name):
    if name == 'Agent':
        from .agent import Agent
        return Agent
    if name == 'MultiAgentHub':
        from .multi_agent_hub import MultiAgentHub
        return MultiAgentHub
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    'Agent',
    'MultiAgentHub',
]
