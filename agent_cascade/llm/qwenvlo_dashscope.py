from typing import Dict, Optional

from agent_cascade.llm.base import register_llm
from agent_cascade.llm.qwenvl_dashscope import QwenVLChatAtDS


@register_llm('qwenvlo_dashscope')
class QwenVLoChatAtDS(QwenVLChatAtDS):

    @property
    def support_multimodal_output(self) -> bool:
        return True

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        self.model = self.model or 'qwen-audio-turbo-latest'
