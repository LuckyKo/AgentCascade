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
import json
import os
import pprint
import re
from typing import List, Optional, Union

from agent_cascade import Agent, MultiAgentHub
from agent_cascade.agents.user_agent import PENDING_USER_INPUT
from agent_cascade.tools import TOOL_REGISTRY
from agent_cascade.gui.gradio_utils import format_cover_html
from agent_cascade.gui.utils import convert_fncall_to_text, convert_history_to_chatbot, get_avatar_image
from agent_cascade.llm.schema import ASSISTANT, AUDIO, CONTENT, FILE, IMAGE, NAME, ROLE, SYSTEM, USER, VIDEO, Message
from agent_cascade.log import logger
from agent_cascade.utils.utils import print_traceback


class WebUI:
    """A Common chatbot application for agent."""

    def __init__(self, agent: Union[Agent, MultiAgentHub, List[Agent]], chatbot_config: Optional[dict] = None):
        """
        Initialization the chatbot.

        Args:
            agent: The agent or a list of agents,
                supports various types of agents such as Assistant, GroupChat, Router, etc.
            chatbot_config: The chatbot configuration.
                Set the configuration as {'user.name': '', 'user.avatar': '', 'agent.avatar': '', 'input.placeholder': '', 'prompt.suggestions': []}.
        """
        chatbot_config = chatbot_config or {}

        if isinstance(agent, MultiAgentHub):
            self.agent_list = [agent for agent in agent.nonuser_agents]
            self.agent_hub = agent
        elif isinstance(agent, list):
            self.agent_list = agent
            self.agent_hub = None
        else:
            self.agent_list = [agent]
            self.agent_hub = None

        user_name = chatbot_config.get('user.name', 'user')
        self._last_active_sa = None
        self._last_rendered_sa = None  # Track what agent was last rendered in sub-chatbot

        self.user_config = {
            'name': user_name,
            'avatar': chatbot_config.get(
                'user.avatar',
                get_avatar_image(user_name),
            ),
        }

        self.agent_config_list = [{
            'name': agent.name,
            'avatar': chatbot_config.get(
                'agent.avatar',
                get_avatar_image(agent.name),
            ),
            'description': agent.description or "I'm a helpful assistant.",
        } for agent in self.agent_list]

        self.input_placeholder = chatbot_config.get('input.placeholder', '跟我聊聊吧～')
        self.prompt_suggestions = chatbot_config.get('prompt.suggestions', [])
        self.verbose = chatbot_config.get('verbose', False)
        
        # Store original function maps for tool toggling
        self.original_function_maps = {}
        for i, agent in enumerate(self.agent_list):
            if hasattr(agent, 'function_map') and agent.function_map:
                self.original_function_maps[i] = dict(agent.function_map)
        
        # Build a master list of ALL tools across all agents
        self.all_available_tools = set()
        for i, agent in enumerate(self.agent_list):
            if hasattr(agent, 'function_map') and agent.function_map:
                self.all_available_tools.update(agent.function_map.keys())
        
        # Add explicitly available tools from config
        config_available_tools = chatbot_config.get('available_tools', [])
        if config_available_tools:
            self.all_available_tools.update(config_available_tools)
            
        self.all_available_tools = sorted(list(self.all_available_tools))
        self._sanitized_cache = {}

    """
    Run the chatbot.

    Args:
        messages: The chat history.
    """

    def run(self,
            messages: List[Message] = None,
            share: bool = False,
            server_name: str = None,
            server_port: int = None,
            concurrency_limit: int = 10,
            enable_mention: bool = False,
            **kwargs):
        self.run_kwargs = kwargs

        from agent_cascade.gui.gradio_dep import gr, mgr, ms

        customTheme = gr.themes.Default(
            primary_hue=gr.themes.utils.colors.blue,
            radius_size=gr.themes.utils.sizes.radius_none,
        )

        with gr.Blocks(
                css=os.path.join(os.path.dirname(__file__), 'assets/appBot.css'),
                theme=customTheme,
        ) as demo:
            history = gr.State([])
            sub_agent_history = gr.State([])  # Track sub-agent conversations
            slot_last_active = gr.State({})  # Track last activity time for LRU eviction
            
            with ms.Application():
                with gr.Row(elem_classes='container'):
                    with gr.Column(scale=3):  # Main chat - slightly smaller
                        chatbot = mgr.Chatbot(value=convert_history_to_chatbot(messages=messages),
                                              avatar_images=[
                                                  self.user_config,
                                                  self.agent_config_list,
                                              ],
                                              height=700,
                                              avatar_image_width=80,
                                              flushing=False,
                                              show_copy_button=True,
                                              label='Main Chat',
                                              latex_delimiters=[{
                                                  'left': '\\(',
                                                  'right': '\\)',
                                                  'display': True
                                              }, {
                                                  'left': '\\begin{equation}',
                                                  'right': '\\end{equation}',
                                                  'display': True
                                              }, {
                                                  'left': '\\begin{align}',
                                                  'right': '\\end{align}',
                                                  'display': True
                                              }, {
                                                  'left': '\\begin{alignat}',
                                                  'right': '\\end{alignat}',
                                                  'display': True
                                              }, {
                                                  'left': '\\begin{gather}',
                                                  'right': '\\end{gather}',
                                                  'display': True
                                              }, {
                                                  'left': '\\begin{CD}',
                                                  'right': '\\end{CD}',
                                                  'display': True
                                              }, {
                                                  'left': '\\[',
                                                  'right': '\\]',
                                                  'display': True
                                              }])

                        input = mgr.MultimodalInput(placeholder=self.input_placeholder,)
                        with gr.Row():
                            stop_btn = gr.Button("⏹️ Stop", variant="secondary")
                            retry_btn = gr.Button("🔄 Retry", variant="secondary")
                            reset_btn = gr.Button("🗑️ Reset Chat", variant="danger")
                        
                        gr.Markdown("---")
                        gr.Markdown("### ⚡ Async Steering (Always Active)")
                        with gr.Row():
                            async_steering_box = gr.Textbox(
                                label="Urgent Message / Interruption",
                                placeholder="Type here to inject a message even while the agent is generating...",
                                lines=1,
                                scale=4
                            )
                            async_steering_btn = gr.Button("Inject", variant="primary", scale=1)
                        gr.Markdown("---")
                        
                        active_stack_out = gr.JSON(label="Active Agent Stack", value=[], visible=False)
                        audio_input = gr.Audio(
                            sources=["microphone"],
                            type="filepath"
                        )
                    
                    # Sub-agent conversation panel
                    NUM_SUB_SLOTS = 12
                    sub_tabs = []
                    sub_chatbots = []
                    sub_statuses = []
                    
                    with gr.Column(scale=2, visible=(len(self.agent_list) > 1)) as sub_agent_panel:
                        slot_map = gr.State({}) # Keep track of which instance_name maps to which index (0-4)
                        with gr.Tabs() as sub_agent_tabs:
                            for i in range(NUM_SUB_SLOTS):
                                with gr.TabItem(f"Slot {i+1}", visible=False, id=f"sub_tab_{i}") as tab:
                                    sub_tabs.append(tab)
                                    cb = gr.Chatbot(value=[], label='Sub-Agent Activity', height=700, show_copy_button=True, type="tuples", show_share_button=False)
                                    sub_chatbots.append(cb)
                                    st = gr.Textbox(label='Status', value='Ready', interactive=False, lines=2)
                                    sub_statuses.append(st)

                    with gr.Column(scale=1):
                        if len(self.agent_list) > 1:
                            agent_selector = gr.Dropdown(
                                [(agent.name, i) for i, agent in enumerate(self.agent_list)],
                                label='Agents',
                                info='Agent',
                                value=0,
                                interactive=True,
                            )
                        else:
                            agent_selector = gr.State(0)

                        session_name = gr.Textbox(
                            label='Session Name',
                            placeholder='Enter session identifier (e.g. CoderSession)',
                            value='Maine',
                            interactive=True,
                            elem_id="session_name_input"
                        )

                        # --- User Approval Panel (polls for blocking approvals) ---
                        with gr.Accordion("🛡️ Pending Approvals", open=True, visible=False, elem_id="approval_panel") as approval_accordion:
                            approval_id_list = gr.Dropdown(
                                label="Select Request",
                                choices=[],
                                interactive=True
                            )
                            approval_details = gr.Markdown("No pending requests.")
                            with gr.Row():
                                approve_btn = gr.Button("✅ Approve", variant="primary")
                                reject_btn = gr.Button("❌ Reject", variant="stop")
                            reject_reason_input = gr.Textbox(
                                label="Rejection reason (required)",
                                placeholder="Why are you rejecting this operation?",
                                visible=False,
                                lines=2,
                            )
                            # Control settings for approvals
                            timeout_toggle = gr.Checkbox(label="Enable 5-minute AFK auto-reject timeout (if unchecked, it will wait forever)", value=True)
                            
                            #timer to poll for pending approvals every 1 second
                            approval_timer = gr.Timer(value=1, active=True)
                        
                        # --- Load Session Panel ---
                        with gr.Accordion("📂 Load Session", open=False):
                            log_load_file = gr.File(
                                label="Upload Log File (.jsonl)",
                                file_types=[".jsonl"],
                                file_count="single"
                            )
                            load_session_btn = gr.Button("🚀 Load Session", variant="secondary")
                            load_status = gr.Markdown("")
                        
                        agent_plugins_block = self._create_agent_plugins_block()

                        # Add event handler for tool toggle
                        if len(self.agent_list) > 1:
                            agent_plugins_block.change(
                                fn=self.toggle_tools,
                                inputs=[agent_plugins_block, agent_selector],
                                outputs=[agent_plugins_block],
                                queue=False,
                            )
                        else:
                            # Single agent case - use constant 0 for agent index
                            agent_plugins_block.change(
                                fn=self.toggle_tools,
                                inputs=[agent_plugins_block],
                                outputs=[agent_plugins_block],
                                queue=False,
                            )

                        if len(self.agent_list) > 1:
                            agent_selector.change(
                                fn=self.change_agent,
                                inputs=[agent_selector, slot_map] + sub_chatbots + sub_statuses,
                                outputs=[agent_selector, agent_plugins_block, slot_map, sub_agent_tabs] + sub_tabs + sub_chatbots + sub_statuses,
                                queue=False,
                            )

                    input_promise = input.submit(
                        fn=self.add_text,
                        inputs=[input, audio_input, chatbot, history, slot_map, slot_last_active] + sub_chatbots + sub_statuses,
                        outputs=[input, audio_input, chatbot, history, slot_map, slot_last_active, sub_agent_tabs] + sub_tabs + sub_chatbots + sub_statuses,
                        queue=True,
                    )

                    if len(self.agent_list) > 1 and enable_mention:
                        input_promise = input_promise.then(
                            self.add_mention,
                            [chatbot, agent_selector],
                            [chatbot, agent_selector],
                            queue=True,
                        ).then(
                            self.agent_run,
                            [chatbot, history, agent_selector, slot_map, slot_last_active] + sub_chatbots + sub_statuses + [session_name],
                            [chatbot, history, agent_selector, slot_map, slot_last_active, sub_agent_tabs] + sub_tabs + sub_chatbots + sub_statuses,
                            queue=True,
                        )
                    elif len(self.agent_list) > 1:
                        # Multiple agents but mention disabled - still pass agent_selector
                        input_promise = input_promise.then(
                            self.agent_run,
                            [chatbot, history, agent_selector, slot_map, slot_last_active] + sub_chatbots + sub_statuses + [session_name],
                            [chatbot, history, agent_selector, slot_map, slot_last_active, sub_agent_tabs] + sub_tabs + sub_chatbots + sub_statuses,
                            queue=True,
                        )
                    else:
                        input_promise = input_promise.then(
                            self.agent_run,
                            [chatbot, history, agent_selector, slot_map, slot_last_active] + sub_chatbots + sub_statuses + [session_name],
                            [chatbot, history, agent_selector, slot_map, slot_last_active, sub_agent_tabs] + sub_tabs + sub_chatbots + sub_statuses,
                            queue=True,
                        )

                    input_promise.then(self.flushed, None, [input])

                    # --- Event Handlers for Approvals ---
                    # Timer-based polling for pending approvals
                    approval_timer.tick(
                        fn=self._update_approval_list,
                        outputs=[approval_id_list, approval_details, approval_accordion]
                    )

                    approval_id_list.change(
                        fn=self._update_approval_details,
                        inputs=[approval_id_list],
                        outputs=[approval_details, reject_reason_input]
                    )

                    approve_btn.click(
                        fn=self._handle_approve,
                        inputs=[approval_id_list],
                        outputs=[approval_id_list, approval_details, approval_accordion]
                    )

                    reject_btn.click(
                        fn=self._handle_reject,
                        inputs=[approval_id_list, reject_reason_input],
                        outputs=[approval_id_list, approval_details, approval_accordion, reject_reason_input]
                    )
                    
                    timeout_toggle.change(
                        fn=self._handle_timeout_toggle,
                        inputs=[timeout_toggle],
                        outputs=None
                    )

                    # --- Event Handlers for Stop/Retry/Reset ---
                    stop_btn.click(
                        fn=self.stop_chat,
                        inputs=[agent_selector],
                        outputs=None,
                        cancels=[input_promise],
                        queue=False
                    )
                    
                    retry_promise = retry_btn.click(
                        fn=self.retry_chat,
                        inputs=[chatbot, history, agent_selector, slot_map, slot_last_active] + sub_chatbots + sub_statuses + [session_name],
                        outputs=[chatbot, history, agent_selector, slot_map, slot_last_active, sub_agent_tabs] + sub_tabs + sub_chatbots + sub_statuses,
                    )
                    
                    reset_btn.click(
                        fn=self.reset_chat,
                        inputs=[agent_selector, slot_map, slot_last_active] + sub_chatbots + sub_statuses,
                        outputs=[chatbot, history, agent_selector, slot_map, slot_last_active, sub_agent_tabs] + sub_tabs + sub_chatbots + sub_statuses,
                        queue=False,
                    )

                    load_session_btn.click(
                        fn=self.handle_load_session,
                        inputs=[log_load_file, session_name, agent_selector, slot_map, slot_last_active] + sub_chatbots + sub_statuses,
                        outputs=[load_status, chatbot, history, session_name, agent_selector, slot_map, slot_last_active, sub_agent_tabs] + sub_tabs + sub_chatbots + sub_statuses,
                    ).then(
                        fn=lambda: None,
                        outputs=[log_load_file]
                    )

                    # --- Event Handlers for Async Injection ---
                    def inject_async(text, agent_sel, session_name_val, chatbot_value):
                        if not text.strip():
                            # Get current active stack if possible
                            active_stack = getattr(self.agent_list[0], 'agent_pool', None).active_stack if hasattr(self.agent_list[0], 'agent_pool') else []
                            return "", active_stack, chatbot_value
                        
                        # Determine the correct instance name for logging.
                        # agent_sel is the index from agent_selector (Main is 0).
                        # If it's the main agent (0), use the custom session name (e.g. 'Maine').
                        if agent_sel == 0:
                            instance_to_log = session_name_val
                        else:
                            instance_to_log = self.agent_list[agent_sel].name
                            
                        # Get the agent pool from the main agent
                        _agent = self.agent_list[0]
                        _agent_pool = getattr(_agent, 'agent_pool', None)
                        if not _agent_pool:
                            return "", [], chatbot_value
                            
                        # 1. Update the in-memory active conversation if it exists
                        if instance_to_log in _agent_pool.instance_conversations:
                            _agent_pool.instance_conversations[instance_to_log].append(Message(role=USER, content=text))
                        
                        # 2. Log to the persistent history file
                        logger_inst = _agent_pool.get_logger(instance_to_log, "Orchestrator")
                        logger_inst.log_message(Message(role=USER, content=text))
                        
                        # 3. Append to the queue for potential mid-turn injection
                        _agent_pool.async_message_queue.append(text)
                        print(f"Injecting async message into {instance_to_log}: {text}")
                        
                        # 4. Immediately update the chatbot UI
                        new_chatbot = chatbot_value + [(text, None)]
                        return "", _agent_pool.active_stack, new_chatbot

                    async_steering_btn.click(
                        fn=inject_async,
                        inputs=[async_steering_box, agent_selector, session_name, chatbot],
                        outputs=[async_steering_box, active_stack_out, chatbot],
                        queue=False
                    )

                    async_steering_box.submit(
                        fn=inject_async,
                        inputs=[async_steering_box, agent_selector, session_name, chatbot],
                        outputs=[async_steering_box, active_stack_out, chatbot],
                        queue=False
                    )

            demo.load(None)

        demo.queue(default_concurrency_limit=concurrency_limit).launch(share=share,
                                                                       server_name=server_name,
                                                                       server_port=server_port)

    def _parse_slots(self, args):
        from agent_cascade.gui.gradio_dep import gr
        num_slots = 12
        # Since Tabs cannot be inputs, they are no longer in args.
        # We return gr.update() for them so they can be in outputs.
        tabs = [gr.update() for _ in range(num_slots)]
        tabs_container = gr.update()  # For the gr.Tabs
        chatbots = list(args[:num_slots])
        statuses = list(args[num_slots:2*num_slots])
        remaining = args[2*num_slots:]
        return tabs, tabs_container, chatbots, statuses, remaining

    def _sanitize_content(self, text: str) -> str:
        """Prevent Gradio crash by disabling links to local directories with memoization."""
        if not text or not isinstance(text, str):
            return text
            
        # Fast path 1: Check if this exact string is already sanitized
        if text in self._sanitized_cache:
            return self._sanitized_cache[text]
            
        # Fast path 2: If there are no absolute paths or file links, skip regex processing
        if ':' not in text and '/' not in text:
            return text

        def is_dir(path):
            try:
                # Remove common local prefixes
                clean_path = path
                if clean_path.startswith('file://'):
                    clean_path = clean_path[7:]
                
                # Strip leading slash on Windows if it's like /C:/
                if os.name == 'nt' and clean_path.startswith('/') and len(clean_path) > 2 and clean_path[2] == ':':
                    clean_path = clean_path[1:]

                # Only call isdir if it's an absolute path
                if not os.path.isabs(clean_path):
                    return False
                return os.path.exists(clean_path) and os.path.isdir(clean_path)
            except:
                return False

        # 1. Handle explicit markdown links [label](path)
        def replace_dir_link(match):
            label = match.group(1)
            path = match.group(2)
            if is_dir(path):
                return f"📂 **{label}** (Directory: `{path}`)"
            return match.group(0)

        # Regex for markdown links: [label](path)
        processed = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', replace_dir_link, text)
        
        # 2. Handle raw absolute paths
        def replace_raw_dir(match):
            path = match.group(0)
            punct = ""
            while path and path[-1] in '.,;:!?)]':
                punct = path[-1] + punct
                path = path[:-1]
                
            if is_dir(path):
                return f"`{path}`{punct}"
            return match.group(0)

        raw_path_pattern = r'(?:[a-zA-Z]:\\[^\s"\'<>|]+|/(?:[^/\s"\'<>|]+/)+[^\s"\'<>|]*)'
        sanitized = re.sub(raw_path_pattern, replace_raw_dir, processed)
        
        # Cache management: avoid memory leaks if cache grows too large
        if len(self._sanitized_cache) > 2000: # Larger cache limit
            self._sanitized_cache.clear()
        self._sanitized_cache[text] = sanitized
        
        return sanitized

    def change_agent(self, agent_selector, _slot_map, *args):
        # Restore original function map when switching agents
        if agent_selector in self.original_function_maps:
            self.agent_list[agent_selector].function_map = self.original_function_maps[agent_selector]
        
        tabs, tabs_container, sub_chatbots, sub_statuses, remaining = self._parse_slots(args)

        # [agent_selector, agent_plugins_block, slot_map, sub_agent_tabs] + sub_tabs + sub_chatbots + sub_statuses
        res = [
            agent_selector,
            self._get_agent_tools_update(agent_selector),
            _slot_map,
            tabs_container
        ]
        res.extend(tabs)
        res.extend(sub_chatbots)
        res.extend(sub_statuses)
        yield tuple(res)

    def toggle_tools(self, selected_tools, agent_selector=None):
        """Update the agent's available tools based on user selection with cross-agent discovery."""
        if agent_selector is None:
            agent_selector = 0
        
        agent = self.agent_list[agent_selector]
        if hasattr(agent, 'function_map'):
            new_map = {}
            for t_name in selected_tools:
                # 1. Try to restore from this agent's original map
                if agent_selector in self.original_function_maps and t_name in self.original_function_maps[agent_selector]:
                    new_map[t_name] = self.original_function_maps[agent_selector][t_name]
                # 2. Try to discover from other agents
                else:
                    discovered_tool = None
                    for other_idx, other_map in self.original_function_maps.items():
                        if t_name in other_map:
                            # Found it! Copy the tool instance
                            discovered_tool = copy.copy(other_map[t_name])
                            
                            # Reconfigure for current agent if it has an agent_name attribute
                            if hasattr(discovered_tool, 'agent_name'):
                                discovered_tool.agent_name = agent.name
                            break
                    
                    if discovered_tool:
                        new_map[t_name] = discovered_tool
                    # 3. Last resort: Instantiate from registry
                    elif t_name in TOOL_REGISTRY:
                        try:
                            # Create new instance of the tool
                            tool_class = TOOL_REGISTRY[t_name]
                            new_tool = tool_class()
                            
                            # Configure for current agent if it has an agent_name attribute
                            if hasattr(new_tool, 'agent_name'):
                                new_tool.agent_name = agent.name
                            
                            new_map[t_name] = new_tool
                        except Exception as e:
                            logger.error(f"Failed to dynamically instantiate tool {t_name}: {e}")
            
            # If no tools selected, Qwen framework usually wants all restored or empty?
            # User expectation: if I uncheck everything, agent has no tools.
            # But if I check one, it should have ONLY that one.
            agent.function_map = new_map
            
        yield self._get_agent_tools_update(agent_selector)

        # [input, audio_input, chatbot, history, slot_map, sub_agent_tabs] + sub_tabs + sub_chatbots + sub_statuses
        res = [gr.update(interactive=False, value=None), None, _chatbot, _history, _slot_map, tabs_container]
        res.extend(tabs)
        res.extend(sub_chatbots)
        res.extend(sub_statuses)
        yield tuple(res)

    def add_text(self, _input, _audio_input, _chatbot, _history, _slot_map, _slot_last_active, *args):
        _history.append({
            ROLE: USER,
            CONTENT: [{
                'text': _input.text
            }],
        })

        if self.user_config[NAME]:
            _history[-1][NAME] = self.user_config[NAME]
        
        # if got audio from microphone, append it to the multimodal inputs
        if _audio_input:
            from agent_cascade.gui.gradio_dep import gr, mgr, ms
            audio_input_file = gr.data_classes.FileData(path=_audio_input, mime_type="audio/wav")
            _input.files.append(audio_input_file)

        if _input.files:
            for file in _input.files:
                if file.mime_type.startswith('image/'):
                    _history[-1][CONTENT].append({IMAGE: 'file://' + file.path})
                elif file.mime_type.startswith('audio/'):
                    _history[-1][CONTENT].append({AUDIO: 'file://' + file.path})
                elif file.mime_type.startswith('video/'):
                    _history[-1][CONTENT].append({VIDEO: 'file://' + file.path})
                else:
                    _history[-1][CONTENT].append({FILE: file.path})

        _chatbot.append([_input, None])

        from agent_cascade.gui.gradio_dep import gr
        tabs, tabs_container, sub_chatbots, sub_statuses, remaining = self._parse_slots(args)

        # [input, audio_input, chatbot, history, slot_map, sub_agent_tabs] + sub_tabs + sub_chatbots + sub_statuses
        res = [gr.update(interactive=False, value=None), None, _chatbot, _history, _slot_map, _slot_last_active, tabs_container]
        res.extend(tabs)
        res.extend(sub_chatbots)
        res.extend(sub_statuses)
        yield tuple(res)

    def add_mention(self, _chatbot, _agent_selector):
        if len(self.agent_list) == 1:
            yield _chatbot, _agent_selector

        query = _chatbot[-1][0].text
        match = re.search(r'@\w+\b', query)
        if match:
            _agent_selector = self._get_agent_index_by_name(match.group()[1:])

        agent_name = self.agent_list[_agent_selector].name

        if ('@' + agent_name) not in query and self.agent_hub is None:
            _chatbot[-1][0].text = '@' + agent_name + ' ' + query

        yield _chatbot, _agent_selector

    def reset_chat(self, _agent_selector, _slot_map, _slot_last_active, *args):
        """Reset the conversation state."""
        from agent_cascade.gui.gradio_dep import gr
        # Clear main history and chatbot
        _history = []
        _chatbot = []
        _slot_map = {} # Clear slot mapping
        _slot_last_active = {} # Clear LRU tracking
        
        # If we have an OrchestratorAgent, clearing the internal history is important
        if _agent_selector is not None:
            agent = self.agent_list[_agent_selector]
            _agent_pool = getattr(agent, 'agent_pool', None)
            if _agent_pool:
                # Full reset of all sub-agent instances and loggers in the pool
                _agent_pool.reset()

        tabs, tabs_container, sub_chatbots, sub_statuses, remaining = self._parse_slots(args)
        
        # Reset all slots to invisible and empty
        res = [_chatbot, _history, _agent_selector, _slot_map, _slot_last_active, tabs_container]
        
        # Correctly align with outputs structure: [tabs...] then [chatbots...] then [statuses...]
        res.extend([gr.update(visible=False, label=f"Slot {i+1}") for i in range(len(tabs))])
        res.extend([[] for _ in range(len(sub_chatbots))])
        res.extend(["Ready" for _ in range(len(sub_statuses))])
            
        return tuple(res)

    def stop_chat(self, _agent_selector=None):
        """Signal all agents to stop execution."""
        agent_runner = self.agent_list[_agent_selector or 0]
        if self.agent_hub:
            agent_runner = self.agent_hub
            
        _agent_pool = getattr(agent_runner, 'agent_pool', None)
        if _agent_pool:
            logger.info("STOP button clicked. Signalling cancellation.")
            _agent_pool.stopped = True
        return None

    def retry_chat(self, _chatbot, _history, _agent_selector, _slot_map, _slot_last_active, *args):
        """Remove last response and re-run."""
        if not _history or len(_history) < 2:
            # Reconstruct the outputs for Gradio
            tabs, tabs_container, sub_chatbots, sub_statuses, remaining = self._parse_slots(args)
            res = [_chatbot, _history, _agent_selector, _slot_map, _slot_last_active, tabs_container]
            res.extend(tabs)
            res.extend(sub_chatbots)
            res.extend(sub_statuses)
            yield tuple(res)
            return

        # Remove the last ASSISTANT message and any trailing metadata
        if _history[-1][ROLE] == ASSISTANT:
            _history.pop()
        
        # Remove the last bubble from chatbot if it was an assistant response
        if _chatbot and _chatbot[-1][1] is not None:
            _chatbot.pop()

        yield from self.agent_run(_chatbot, _history, _agent_selector, _slot_map, _slot_last_active, *args)

    def agent_run(self, _chatbot, _history, _agent_selector, _slot_map, _slot_last_active, *args):
        from agent_cascade.gui.gradio_dep import gr
        
        # Parse slot components and potential session name
        tabs, tabs_container, sub_chatbots, sub_statuses, remaining = self._parse_slots(args)
        _session_name = remaining[0] if remaining else "Maine"
        
        if self.verbose:
            logger.info('agent_run input:\n' + pprint.pformat(_history, indent=2))

        # Capture expected structure at the start
        has_sub = len(self.agent_list) > 1 or self.agent_hub is not None
        has_selector = len(self.agent_list) > 1
        
        num_input_bubbles = len(_chatbot) - 1
        num_output_bubbles = 1
        _chatbot[-1][1] = [None for _ in range(len(self.agent_list))]

        agent_runner = self.agent_list[_agent_selector or 0]
        if self.agent_hub:
            agent_runner = self.agent_hub
        
        if hasattr(agent_runner, 'session_name'):
            agent_runner.session_name = _session_name
        
        _agent_pool = getattr(agent_runner, 'agent_pool', None)
        if _agent_pool:
            _agent_pool.stopped = False
            self._last_active_sa = None
            if hasattr(_agent_pool, 'active_stack'):
                _agent_pool.active_stack.clear()
        
        main_label = f"Main Chat: {agent_runner.__class__.__name__}"
        if hasattr(agent_runner, 'session_name'):
            main_label += f" ({agent_runner.session_name})"
        elif hasattr(agent_runner, 'name'):
            main_label += f" ({agent_runner.name})"

        # Initial yield to clear and update. 
        # Using shallow copy for the main list and deepcopy ONLY for the active bubble(s) that might be mutated.
        def get_all_outputs():
            # Optimization: avoid expensive deepcopy on the entire chatbot history.
            # Gradio's mgr.Chatbot generally expects a value that it doesn't mutate, or it handles it.
            # However, we often mutate the *last* bubble in _chatbot during streaming.
            
            _chatbot_val = list(_chatbot) # Shallow copy of the list
            if _chatbot_val:
                # Deepcopy the last bubble since it's most likely being mutated during streaming
                _chatbot_val[-1] = copy.deepcopy(_chatbot_val[-1])
            
            res = [
                gr.update(value=_chatbot_val, label=main_label), 
                _history,
                _agent_selector,
                _slot_map,
                _slot_last_active,
                tabs_container
            ]
            res.extend(tabs)
            res.extend(sub_chatbots)
            res.extend(sub_statuses)
            return tuple(res)

        yield get_all_outputs()
        
        _prev_rsp_count = 0
        agent_index = _agent_selector or 0
        responses = []
        import time
        last_yield_time = 0
        yield_interval = 0.1  # 10Hz throttle
        
        _last_stack_top = None
        _last_responses_key = None
        
        try:
            for responses in (agent_runner.run(_history, **self.run_kwargs) if hasattr(agent_runner, "run") else []):
                current_time = time.time()
                is_agent_switch = False
                main_changed = False
                sub_changed = False
                
                if self.verbose: logger.info(f"[DEBUG] agent_run tick at {current_time}")
                
                # 0. Cleanup check: Remove any instances from slot_map that no longer exist in the pool
                # (e.g. dismissed agents)
                if has_sub and _agent_pool:
                    dead_instances = [inst for inst in _slot_map if inst not in _agent_pool.instance_conversations]
                    for inst in dead_instances:
                        s_idx = _slot_map.pop(inst)
                        tabs[s_idx] = gr.update(visible=False, label=f"Slot {s_idx+1}")
                        sub_chatbots[s_idx] = []
                        sub_statuses[s_idx] = "Ready"
                        _slot_last_active.pop(s_idx, None)
                        sub_changed = True
                        if self.verbose: logger.info(f"[DEBUG] Slot {s_idx} freed as instance {inst} was dismissed.")
                
                active_slot_idx = None
                if has_sub and _agent_pool and hasattr(_agent_pool, 'sub_agent_state'):
                    active_stack = getattr(_agent_pool, 'active_stack', [])
                    current_top = active_stack[-1] if active_stack else None

                    if current_top:
                        # Find or allocate a slot for this sub-agent
                        if current_top not in _slot_map:
                            # 1. Look for a free slot
                            free_slot = None
                            for i in range(len(tabs)):
                                if i not in _slot_map.values():
                                    free_slot = i
                                    break
                            
                            # 2. If no free slot, use LRU eviction
                            if free_slot is None:
                                # Find slot with oldest last_active time
                                oldest_slot = 0
                                min_time = float('inf')
                                for s_idx in range(len(tabs)):
                                    l_a_time = _slot_last_active.get(s_idx, 0)
                                    if l_a_time < min_time:
                                        min_time = l_a_time
                                        oldest_slot = s_idx
                                
                                # Unmap previous owner
                                owners_to_remove = [k for k, v in _slot_map.items() if v == oldest_slot]
                                for k in owners_to_remove:
                                    del _slot_map[k]
                                
                                free_slot = oldest_slot
                                if self.verbose: logger.info(f"[DEBUG] LRU Eviction: Reusing slot {free_slot} for {current_top}")

                            _slot_map[current_top] = free_slot
                            # Make tab visible, focused and update label
                            tabs[free_slot] = gr.update(visible=True, label=f"🔄 {current_top}")
                            tabs_container = gr.update(selected=f"sub_tab_{free_slot}")
                            # Clear its chatbot for new session
                            sub_chatbots[free_slot] = []
                        
                        active_slot_idx = _slot_map.get(current_top)
                        if active_slot_idx is not None:
                            # Update LRU time
                            _slot_last_active[active_slot_idx] = time.time()
                            
                            # Switch focus if it's a new top agent
                            if current_top != _last_stack_top:
                                is_agent_switch = True
                                _last_stack_top = current_top
                                tabs_container = gr.update(selected=f"sub_tab_{active_slot_idx}")

                            # Update the slot's content with memoization
                            sa_state = _agent_pool.sub_agent_state[current_top]
                            messages = sa_state.get('messages', [])
                            
                            # Generate a unique key for this history state to avoid redundant formatting
                            last_msg = messages[-1] if messages else None
                            msg_key = (
                                len(messages), 
                                id(last_msg), # id() is somewhat stable for long-lived objects in list
                                last_msg.get(CONTENT) if isinstance(last_msg, dict) else getattr(last_msg, CONTENT, None) if last_msg else None,
                                last_msg.get('function_call') if isinstance(last_msg, dict) else getattr(last_msg, 'function_call', None) if last_msg else None
                            )
                            
                            # Check cache for this slot
                            cached_key = getattr(self, f"_last_msg_key_{active_slot_idx}", None)
                            if cached_key != msg_key:
                                setattr(self, f"_last_msg_key_{active_slot_idx}", msg_key)
                                
                                if self.verbose: logger.info(f"[DEBUG] Sub-agent {current_top} state: active={sa_state.get('active')}, msg_count={len(messages)}")
                                sub_statuses[active_slot_idx] = f"{current_top} is responding..." if sa_state.get('active') else "Finished"
                                
                                # Process history into chatbot bubbles (incremental logic possible here but even this memoization helps)
                                # Deepcopy here is fine because it's only one sub-agent's local history, usually much smaller than main
                                messages_copy = copy.deepcopy(messages)
                                new_val = []
                                if messages_copy:
                                    formatted = convert_fncall_to_text(messages_copy)
                                    pair = [None, None]
                                    for msg in formatted:
                                        role, content = msg.get('role'), msg.get('content')
                                        if role == USER:
                                            if pair[0] is not None: new_val.append(list(pair))
                                            pair = [content, None]
                                        elif role == ASSISTANT:
                                            pair[1] = content
                                            new_val.append(list(pair))
                                            pair = [None, None]
                                    if pair[0] or pair[1]: new_val.append(list(pair))
                                
                                # Sanitize ONLY the bubbles that changed or were added would be better, 
                                # but _sanitize_content is now MUCH faster due to its own cache.
                                for bubble in new_val:
                                    if bubble[0]: bubble[0] = self._sanitize_content(bubble[0])
                                    if bubble[1]: bubble[1] = self._sanitize_content(bubble[1])
                                
                                if sub_chatbots[active_slot_idx] != new_val:
                                    sub_chatbots[active_slot_idx] = new_val
                                    sub_changed = True
                    else:
                        _last_stack_top = None

                # Process main chat
                if responses:
                    last_msg = responses[-1]
                    last_content = last_msg.get(CONTENT) if isinstance(last_msg, dict) else getattr(last_msg, CONTENT, None)
                    last_fn_call = last_msg.get('function_call') if isinstance(last_msg, dict) else getattr(last_msg, 'function_call', None)
                    
                    # Track changes in length, content, OR function call (for streaming tool calls)
                    current_responses_key = (len(responses), last_content, str(last_fn_call))
                    
                    if current_responses_key != _last_responses_key:
                        _last_responses_key = current_responses_key
                        if last_content == PENDING_USER_INPUT:
                            break
                        
                        display_responses = convert_fncall_to_text(responses)
                        if display_responses and display_responses[-1][CONTENT] is not None:
                            _prev_rsp_count = len(responses)
                            while len(display_responses) > num_output_bubbles:
                                _chatbot.append([None, [None for _ in range(len(self.agent_list))]])
                                num_output_bubbles += 1
                            for i, rsp in enumerate(display_responses):
                                agent_index = self._get_agent_index_by_name(rsp[NAME])
                                sanitized = self._sanitize_content(rsp[CONTENT])
                                _chatbot[num_input_bubbles + i][1][agent_index] = sanitized
                            if has_selector: _agent_selector = agent_index
                            main_changed = True

                if main_changed or sub_changed or is_agent_switch:
                    # Apply throttling only to the yield itself
                    if is_agent_switch or (current_time - last_yield_time > yield_interval):
                        last_yield_time = current_time
                        if self.verbose: logger.info(f"[DEBUG] yielding update: main={main_changed}, sub={sub_changed}, switch={is_agent_switch}")
                        yield get_all_outputs()
                    else:
                        if self.verbose: logger.info(f"[DEBUG] skipping yield due to throttle")

            # Final cleanup/compression sync
            if responses:
                _history.extend([res for res in responses if res[CONTENT] != PENDING_USER_INPUT])

            if hasattr(agent_runner, 'turn_final_messages') and agent_runner.turn_final_messages:
                tfm = agent_runner.turn_final_messages
                
                # Check if this is just a sliced view (starts with SYSTEM + <context_summary> USER message)
                # If it's a slice, we should NOT clear the full session history.
                is_slice = False
                if len(tfm) > 1 and len(tfm) < len(_history):
                    msg1_content = tfm[1].get(CONTENT, '') if isinstance(tfm[1], dict) else getattr(tfm[1], 'content', '')
                    if isinstance(msg1_content, str) and "<context_summary>" in msg1_content:
                        is_slice = True
                
                if not is_slice and len(tfm) < len(_history):
                    _history.clear()
                    for res in tfm:
                        msg = res.model_dump() if not isinstance(res, dict) else res
                        if msg.get(ROLE) != SYSTEM: _history.append(msg)
                agent_runner.turn_final_messages = None

            # Reset statuses to Ready
            for i in range(len(sub_statuses)):
                if sub_statuses[i] != "Ready":
                    sub_statuses[i] = "Ready"
            yield get_all_outputs()

        except Exception as e:
            import traceback
            traceback.print_exc()
            error_msg = f"⚠️ **Error:**\n```\n{str(e)}\n```"
            _chatbot.append((None, error_msg))
            yield get_all_outputs()


    def handle_load_session(self, log_file, current_session_name, agent_selector, _slot_map, _slot_last_active, *args):
        """Handle the load session button click."""
        from agent_cascade.gui.gradio_dep import gr
        
        agent_runner = self.agent_list[agent_selector or 0]
        if self.agent_hub:
            agent_runner = self.agent_hub
            
        _agent_pool = getattr(agent_runner, 'agent_pool', None)
        if not _agent_pool:
            # Reconstruct outputs for failure
            tabs, tabs_container, sub_chatbots, sub_statuses, remaining = self._parse_slots(args)
            res = [
                gr.update(value="Error: Current agent does not support session loading.", visible=True),
                gr.update(), # Chatbot
                gr.update(), # History
                current_session_name,
                agent_selector,
                _slot_map,
                _slot_last_active,
                tabs_container
            ]
            res.extend(tabs)
            res.extend(sub_chatbots)
            res.extend(sub_statuses)
            return tuple(res)

        if not log_file:
            # Reconstruct outputs for failure
            tabs, tabs_container, sub_chatbots, sub_statuses, remaining = self._parse_slots(args)
            res = [
                gr.update(value="Error: No file uploaded.", visible=True),
                gr.update(), # Chatbot
                gr.update(), # History
                current_session_name,
                agent_selector,
                _slot_map,
                _slot_last_active,
                tabs_container
            ]
            res.extend(tabs)
            res.extend(sub_chatbots)
            res.extend(sub_statuses)
            return tuple(res)

        # Get the file path from the file object
        # In Gradio, gr.File returns a NamedTemporaryFile-like object or a dict/list of them
        log_path = log_file.name if hasattr(log_file, 'name') else str(log_file)

        # Call backend to load session
        status = _agent_pool.load_session_from_log(log_path, target_instance=current_session_name)
        
        # Prepare components for failure or success
        tabs, tabs_container, sub_chatbots, sub_statuses, remaining = self._parse_slots(args)
        
        if status.startswith("Error"):
            res = [
                gr.update(value=status, visible=True),
                gr.update(), # Chatbot
                gr.update(), # History
                current_session_name,
                agent_selector,
                _slot_map,
                _slot_last_active,
                tabs_container
            ]
            res.extend(tabs)
            res.extend(sub_chatbots)
            res.extend(sub_statuses)
            return tuple(res)

        # Determine which instance was actually loaded
        import re
        match = re.search(r"instance '([^']+)'", status)
        loaded_instance = match.group(1) if match else current_session_name
        
        # Restore history and chatbot
        full_history = _agent_pool.get_conversation(loaded_instance)
        
        # Convert to chatbot format (tuples of [user, assistant])
        new_chatbot = []
        
        # Re-use logic for converting history to chatbot tuples
        formatted = convert_fncall_to_text(full_history)
        pair = [None, None]
        for msg in formatted:
            role, content = msg.get('role'), msg.get('content')
            if role == USER:
                if pair[0] is not None:
                    new_chatbot.append(list(pair))
                pair = [content, None]
            elif role == ASSISTANT:
                pair[1] = content
                new_chatbot.append(list(pair))
                pair = [None, None]
        if pair[0] or pair[1]:
            new_chatbot.append(list(pair))
            
        # Sanitize
        for bubble in new_chatbot:
            if bubble[0]: bubble[0] = self._sanitize_content(bubble[0])
            if bubble[1]: bubble[1] = self._sanitize_content(bubble[1])

        # Update main chat label
        main_label = f"Main Chat: {agent_runner.__class__.__name__} ({loaded_instance})"
        
        res = [
            gr.update(value=status, visible=True),
            gr.update(value=new_chatbot, label=main_label),
            full_history,
            loaded_instance,
            agent_selector, # Added to match outputs
            _slot_map,
            _slot_last_active,
            tabs_container
        ]
        res.extend(tabs)
        res.extend(sub_chatbots)
        res.extend(sub_statuses)
        
        return tuple(res)

    def flushed(self):
        from agent_cascade.gui.gradio_dep import gr

        return gr.update(interactive=True)

    def _get_agent_index_by_name(self, agent_name):
        if agent_name is None:
            return 0

        try:
            agent_name = agent_name.strip()
            for i, agent in enumerate(self.agent_list):
                if agent.name == agent_name:
                    return i
            return 0
        except Exception:
            print_traceback()
            return 0

    def _create_agent_info_block(self, agent_index=0):
        from agent_cascade.gui.gradio_dep import gr

        agent_config_interactive = self.agent_config_list[agent_index]

        return gr.HTML(
            format_cover_html(
                bot_name=agent_config_interactive['name'],
                bot_description=agent_config_interactive['description'],
                bot_avatar=agent_config_interactive['avatar'],
            ))

    def _create_agent_plugins_block(self, agent_index=0):
        from agent_cascade.gui.gradio_dep import gr

        agent_interactive = self.agent_list[agent_index]

        # Show ALL available tools across all agents, not just current agent's tools
        # This allows users to freely enable/disable any tool for any agent
        all_tools = self.all_available_tools
        
        # Get currently enabled tools for this agent
        if hasattr(agent_interactive, 'function_map'):
            enabled_tools = list(agent_interactive.function_map.keys())
        else:
            enabled_tools = []
        
        return gr.CheckboxGroup(
            label='Tools (enable/disable freely)',
            value=enabled_tools,
            choices=all_tools,  # Show all tools from all agents
            interactive=True,
        )

    def _get_agent_tools_update(self, agent_index=0):
        from agent_cascade.gui.gradio_dep import gr
        agent_interactive = self.agent_list[agent_index]
        all_tools = self.all_available_tools
        if hasattr(agent_interactive, 'function_map'):
            enabled_tools = list(agent_interactive.function_map.keys())
        else:
            enabled_tools = []
        return gr.update(value=enabled_tools, choices=all_tools)

    # --- User Approval Helper Methods ---

    def _get_operation_manager(self):
        """Find the OperationManager from the agent pool."""
        for agent in self.agent_list:
            if hasattr(agent, 'agent_pool') and agent.agent_pool:
                return agent.agent_pool.operation_manager
        return None

    def _update_approval_list(self):
        """Poll for pending approvals and update the UI."""
        from agent_cascade.gui.gradio_dep import gr
        manager = self._get_operation_manager()
        if not manager:
            return gr.update(choices=[], value=None), "No approval system.", gr.update(visible=False)

        pending = manager.list_pending_approvals()
        choices = [
            (f"⏳ {op['agent_name']}: {op['tool_name']} - {op['description']}", op['request_id'])
            for op in pending
        ]

        if not choices:
            return gr.update(choices=[], value=None), "No pending requests.", gr.update(visible=False, open=False)

        # Auto-select first if only one
        first_id = choices[0][1] if len(choices) == 1 else None
        return gr.update(choices=choices, value=first_id), gr.update(), gr.update(visible=True, open=True)

    def _update_approval_details(self, request_id):
        """Show details for a specific pending approval."""
        from agent_cascade.gui.gradio_dep import gr
        if not request_id:
            return "Select a request to see details.", gr.update(visible=False)

        manager = self._get_operation_manager()
        if not manager:
            return "Error: OperationManager not found.", gr.update(visible=False)

        # Find the pending approval
        pending = manager.list_pending_approvals()
        req = None
        for p in pending:
            if p['request_id'] == request_id:
                req = p
                break

        if not req:
            return f"Request {request_id} not found or already resolved.", gr.update(visible=False)

        details = f"### 🛡️ Approval Required\n\n"
        details += f"**Agent:** `{req['agent_name']}`\n\n"
        details += f"**Tool:** `{req['tool_name']}`\n\n"
        details += f"**Description:** {req['description']}\n\n"
        details += "**Parameters:**\n"
        details += f"```json\n{json.dumps(req['tool_args'], indent=2)}\n```\n"

        return details, gr.update(visible=True)

    def _handle_approve(self, request_id):
        """User approves a pending operation."""
        from agent_cascade.gui.gradio_dep import gr
        if not request_id:
            return gr.update(), "No request selected.", gr.update()

        manager = self._get_operation_manager()
        if not manager:
            return gr.update(), "Error: OperationManager not found.", gr.update()

        result = manager.user_approve(request_id)

        # Refresh the list
        return self._update_approval_list()

    def _handle_reject(self, request_id, reason):
        """User rejects a pending operation with a reason."""
        from agent_cascade.gui.gradio_dep import gr
        if not request_id:
            return gr.update(), "No request selected.", gr.update(), gr.update()

        if not reason or not reason.strip():
            return gr.update(), "⚠️ **Please provide a reason for rejection.**", gr.update(), gr.update()

        manager = self._get_operation_manager()
        if not manager:
            return gr.update(), "Error: OperationManager not found.", gr.update(), gr.update()

        result = manager.user_reject(request_id, reason.strip())

        # Refresh the list and clear the reason input
        choices_update, details_update, accordion_update = self._update_approval_list()
        return choices_update, details_update, accordion_update, gr.update(value="", visible=False)

    def _handle_timeout_toggle(self, enabled):
        """Update the AFK timeout setting in OperationManager."""
        manager = self._get_operation_manager()
        if manager:
            manager.enable_timeout = enabled
