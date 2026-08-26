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
import logging
from typing import Dict, List, Optional, Union

from agent_cascade.exceptions import AgentTerminatedError
from agent_cascade.llm import get_chat_model
from agent_cascade.llm.schema import USER, ContentItem, Message
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA

logger = logging.getLogger(__name__)


@register_tool('image_gen', allow_overwrite=True)
class ImageGen(BaseTool):
    description = TOOL_METADATA['image_gen']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'prompt': {
                'description': TOOL_METADATA['image_gen']['parameters']['prompt'],
                'type': 'string',
            }
        },
        'required': ['prompt'],
    }

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        llm_cfg = self.cfg.get('llm_cfg', {})
        if not llm_cfg:
            raise ValueError('llm_cfg is required!')
        # ── Change E gate: refuse to construct a chat model for a breaker-open base.
        # Constructing it would fire HTTP (context detection) at the busy server.
        from agent_cascade.llm.oai import _breaker_blocks_base
        if _breaker_blocks_base(llm_cfg.get('api_base') or llm_cfg.get('model_server')):
            raise RuntimeError(
                f"image_gen unavailable: server {llm_cfg.get('api_base')} is busy "
                f"(circuit breaker open) — will retry later."
            )
        self.llm = get_chat_model(llm_cfg)
        self.size = self.cfg.get('size', '1024*1024')

    def call(self, params: Union[str, dict], **kwargs) -> List[ContentItem]:
        if isinstance(params, str):
            params = json.loads(params)

        messages = [Message(role=USER, content=[ContentItem(text=params['prompt'])])]
        kwargs.pop('messages')

        # ── Sticky slot side-call gate (plan change #13 / §3.10 D2). ──
        # ImageGen fires a cold chat request at its configured endpoint. When that
        # endpoint is conc=0, the owning agent's sticky slot must gate it — acquire-or-
        # keep via sync_sticky_slot (check-before-acquire; NEVER drops). The owning
        # instance name arrives through the standard tool-call kwargs
        # (tool_dispatcher passes agent_instance_name); when absent the call proceeds
        # exactly as before (no slot interaction) — same contract as caption_images.
        _ig_cfg = self.cfg.get('llm_cfg', {}) if isinstance(self.cfg, dict) else {}
        _ig_base = _ig_cfg.get('api_base') or _ig_cfg.get('model_server')
        if _ig_base:
            try:
                from agent_cascade.api_router_pkg import breaker_gate
                with breaker_gate._lock:
                    _routers = [ref() for ref in list(breaker_gate._routers)]
            except Exception:
                _routers = []
            router = next((r for r in _routers if getattr(r, '_pool', None) is not None), None)
            if router is not None:
                inst_name = kwargs.get('agent_instance_name') or getattr(self, 'agent_name', None)
                pool = router._pool
                inst = pool.get_instance(inst_name) if inst_name else None
                if inst is not None:
                    try:
                        from agent_cascade.api_router_pkg.normalization import normalize_api_base
                        with router._lock:
                            _ig_conc = None
                            for ep in router.endpoints.values():
                                if ep.enabled and normalize_api_base(ep.api_base) == normalize_api_base(_ig_base) \
                                        and ep.model == _ig_cfg.get('model'):
                                    _ig_conc = ep.concurrency_limit
                                    break
                        # Unmatched endpoint → conservative sequential (mirrors
                        # get_effective_concurrency): the slot must gate this call.
                        if (_ig_conc or 0) == 0:
                            router.sync_sticky_slot(
                                inst,
                                desired_key='_shared_sequential_slot_',
                                origin='sidecall:image_gen',
                            )
                    except AgentTerminatedError:
                        raise
                    except Exception as e:
                        # Sync failure must NOT be swallowed (same policy as
                        # call_with_fallback / caption_images): continuing would fire the
                        # image-gen HTTP at a conc=0 endpoint without holding the shared
                        # slot — ungated. Re-raise so the tool call fails cleanly.
                        logger.error(f"[ImageGen] Sticky slot sync failed for '{inst_name}': {e}", exc_info=True)
                        raise

        *_, last = self.llm.chat(messages=messages)
        return last[-1]['content']
