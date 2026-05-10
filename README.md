<!---
Copyright 2023 The Qwen team, Alibaba Group. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# AgentCascade

[中文](https://github.com/QwenLM/AgentCascade/blob/main/README_CN.md) ｜ English

<p align="center">
    <img src="https://qianwen-res.oss-accelerate-overseas.aliyuncs.com/logo_agent_cascade.png" width="400"/>
<p>
<br>

<p align="center">
          💜 <a href="https://chat.qwen.ai/"><b>Qwen Chat</b></a>&nbsp&nbsp | &nbsp&nbsp🤗 <a href="https://huggingface.co/Qwen">Hugging Face</a>&nbsp&nbsp | &nbsp&nbsp🤖 <a href="https://modelscope.cn/organization/qwen">ModelScope</a>&nbsp&nbsp | &nbsp&nbsp 📑 <a href="https://qwenlm.github.io/">Blog</a> &nbsp&nbsp ｜ &nbsp&nbsp📖 <a href="https://qwenlm.github.io/AgentCascade/en/">Documentation</a>

<br>
📊 <a href="https://qwenlm.github.io/AgentCascade/en/benchmarks/deepplanning/">Benchmark</a>&nbsp&nbsp | &nbsp&nbsp💬 <a href="https://github.com/QwenLM/Qwen/blob/main/assets/wechat.png">WeChat (微信)</a>&nbsp&nbsp | &nbsp&nbsp🫨 <a href="https://discord.gg/CV4E9rpNSD">Discord</a>&nbsp&nbsp
</p>

---

**AgentCascade** is a production-oriented multi-agent framework designed for building robust, scalable, and controllable LLM applications. Originally forked from `Qwen-Agent`, it has evolved into an independent system with a focus on orchestration, security, and developer productivity.

## Core Features

### 🤖 Multi-Agent Orchestration
- **OrchestratorAgent** (`agent_orchestrator.py`): A supervisor agent that manages sub-agent lifecycles, intercepts `call_agent`/`dismiss_agent` tool calls, and handles multi-agent streaming as a unified generator.
- **AgentPool** (`agent_pool.py`): Centralized agent management with per-instance conversation persistence (JSONL), context compression (auto + manual modes), and real-time state synchronization.

### 🛡️ Production Safety & Control
- **OperationManager** (`operation_manager.py`): A mandatory approval system for all mutating operations (file edits, code execution, system commands). Every edit creates timestamped `.bak` backups automatically.
- **Path Isolation**: Strict workspace-relative path resolution ensures agents never access or expose host-absolute paths.
- **Graceful Lifecycle**: Comprehensive signal handling (SIGINT/SIGTERM) ensuring clean shutdowns, backup cleanup, and state preservation.

### 💻 Modern Web UI & API
- **Custom Console** (`web_ui/`): A lightweight, high-performance HTML/JS frontend replacing legacy Gradio interfaces. Supports multi-agent tab switching, approval workflows, and rich tool result rendering.
- **WebSocket API Server** (`api_server.py`): A headless backend allowing any external interface (Electron, VS Code extensions, CLI) to control the agent cluster.

### 🛠️ Robust Tooling
- **XML-Based Tool Protocols**: Large text payloads (code, file contents) are handled via XML tags to eliminate JSON escaping corruptions.
- **PythonExecutor Improvements**: Hardened execution with proper exception isolation and batch crash recovery.
- **Multi-Modal Support**: Native handling of images and files within tool results, including backend proxying for local file rendering.

---

## News
* 🔥🔥🔥Feb 16, 2026: Open-sourced Qwen3.5. For usage examples, refer to [Qwen3.5 Agent Demo](./examples/assistant_qwen3.5.py).
* Jan 27, 2026: Open-sourced agent evaluation benchmark [DeepPlanning](https://qwenlm.github.io/AgentCascade/en/benchmarks/deepplanning/) and added AgentCascade [documentation](https://qwenlm.github.io/AgentCascade/en/guide/).
* Sep 23, 2025: Added [Qwen3-VL Tool-call Demo](./examples/cookbook_think_with_images.ipynb), supporting tools such as zoom in, image search, and web search.
* Jul 23, 2025: Add [Qwen3-Coder Tool-call Demo](./examples/assistant_qwen3_coder.py); Added native API tool call interface support, such as using vLLM's built-in tool call parsing.

---

## Getting Started

### Installation

- Install from PyPI:
```bash
pip install -U "agent-cascade[gui,rag,code_interpreter,mcp]"
```

- Install from source for development:
```bash
git clone https://github.com/QwenLM/AgentCascade.git
cd AgentCascade
pip install -e ./"[gui,rag,code_interpreter,mcp]"
```

### Preparation: Model Service

You can use Alibaba Cloud's [DashScope](https://help.aliyun.com/zh/dashscope/developer-reference/quick-start), or deploy your own OpenAI-compatible API (vLLM, Ollama, etc.).

- Set `DASHSCOPE_API_KEY` environment variable if using DashScope.
- For local models, configure the `model_server` endpoint in your agent config.

---

## Developing Your Own Agent

The following example shows how to create an agent with custom tools:

```python
from agent_cascade.agents import Assistant
from agent_cascade.tools.base import BaseTool, register_tool

@register_tool('my_image_gen')
class MyImageGen(BaseTool):
    description = 'AI painting service'
    parameters = [{'name': 'prompt', 'type': 'string', 'required': True}]

    def call(self, params: str, **kwargs) -> str:
        # Implementation...
        return '{"image_url": "..."}'

llm_cfg = {'model': 'qwen-max-latest'}
bot = Assistant(llm=llm_cfg, function_list=['my_image_gen', 'code_interpreter'])

# Run as chatbot
for response in bot.run(messages=[{'role': 'user', 'content': 'draw a dog'}]):
    print(response)
```

---

## FAQ

- **How to use Code Interpreter?**: Ensure Docker is running. The tool writes and executes code in an isolated container.
- **How to use MCP?**: Configure `mcpServers` in your agent config. See [MCP Example](./examples/assistant_mcp_sqlite_bot.py).
- **Does it support Parallel Tool Calls?**: Yes, natively supported via the `nous` prompt template.

---

## Credits & Origin

**AgentCascade** was originally forked from [QwenLM/Qwen-Agent](https://github.com/QwenLM/Qwen-Agent). We are grateful to the Qwen team for providing the powerful foundation that enabled this framework to grow.

---

## Disclaimer

The Docker-based code interpreter is intended for local testing. Always exercise caution when allowing agents to execute code in production environments.
