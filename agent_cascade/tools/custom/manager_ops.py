from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA


@register_tool('list_agents', allow_overwrite=True)
class ListAgents(BaseTool):
    """Soft-deprecated redirect stub for the removed ``list_agents`` tool.

    The class stays registered so stale calls from older prompts or soul files
    resolve gracefully instead of erroring. Agent classes, descriptions, and
    default active tools are now served by ``system_info(help="agents")``;
    live per-instance metrics are available at ``/api/status``.
    """

    name = 'list_agents'
    description = TOOL_METADATA['list_agents']['description']
    parameters = {
        'type': 'object',
        'properties': {},
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        # Retained for call-site compatibility; the redirect stub uses neither.
        self.agent_pool = agent_pool

    def call(self, params: str, **kwargs) -> str:
        return ('DEPRECATED: use system_info(help="agents") for agent '
                'classes/descriptions/default tools; /api/status for live '
                'per-instance metrics.')
