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

中文 ｜ [English](./README.md)

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

**AgentCascade** 是一个面向生产环境的多智能体（Multi-Agent）框架，旨在构建健壮、可扩展且可控的 LLM 应用。它最初 fork 自 `Qwen-Agent`，现已演进为一个独立的系统，重点关注智能体编排、安全控制和开发效率。

## 核心特性

### 🤖 多智能体编排 (Orchestration)
- **OrchestratorAgent** (`agent_orchestrator.py`)：一个主管智能体，负责管理子智能体的生命周期，拦截 `call_agent`/`dismiss_agent` 工具调用，并将多智能体流式输出统一处理。
- **AgentPool** (`agent_pool.py`)：集中化的智能体管理，支持每个实例的会话持久化（JSONL）、上下文压缩（自动+手动模式）以及实时状态同步。

### 🛡️ 生产级安全与控制
- **OperationManager** (`operation_manager.py`)：强制性的操作审批系统，涵盖所有变更操作（文件编辑、代码执行、系统命令）。每次编辑都会自动创建带时间戳的 `.bak` 备份。
- **路径隔离**：严格的工作区相对路径解析，确保智能体永远无法访问或泄露主机绝对路径。
- **优雅的生命周期管理**：完善的信号处理（SIGINT/SIGTERM），确保清理备份并保存状态。

### 💻 现代 Web UI 与 API
- **自定义控制台** (`web_ui/`)：轻量级、高性能的 HTML/JS 前端，取代了旧版的 Gradio 界面。支持多智能体标签切换、审批流和丰富的工具结果渲染。
- **WebSocket API 服务** (`api_server.py`)：无头后端，允许任何外部界面（Electron、VS Code 插件、CLI）连接并控制智能体集群。

### 🛠️ 健壮的工具系统
- **基于 XML 的工具协议**：通过 XML 标签处理大文本负载（代码、文件内容），消除 JSON 转义导致的静默损坏。
- **增强的 PythonExecutor**：具备异常隔离和批量崩溃恢复能力的加固执行引擎。
- **多模态支持**：原生处理工具结果中的图像和文件，支持后端代理渲染本地文件。

---

## 更新
* 🔥🔥🔥Feb 16, 2026: 开源Qwen3.5，调用实例参考 [Qwen3.5 Agent Demo](./examples/assistant_qwen3.5.py)。
* Jan 27, 2026: 开源Agent评测基准[DeepPlanning](https://qwenlm.github.io/AgentCascade/en/benchmarks/deepplanning/)，增加AgentCascade[文档](https://qwenlm.github.io/AgentCascade/en/guide/)。
* Sep 23, 2025: 新增 [Qwen3-VL Tool-call Demo](./examples/cookbook_think_with_images.ipynb)，支持使用抠图、图搜、文搜等工具。
* Jul 23, 2025: 新增 [Qwen3-Coder Tool-call Demo](./examples/assistant_qwen3_coder.py)；新增原生API工具调用接口支持，例如可使用vLLM自带的工具调用解析。

---

## 开始上手

### 安装

- 从 PyPI 安装稳定版本：
```bash
pip install -U "agent-cascade[rag,code_interpreter,mcp]"
```

- 从源码安装开发版本：
```bash
git clone https://github.com/LuckyKo/AgentCascade.git
cd AgentCascade
pip install -e ./"[rag,code_interpreter,mcp]"
```

### 准备：模型服务

您可以使用阿里云 [DashScope](https://help.aliyun.com/zh/dashscope/developer-reference/quick-start)，或者部署您自己的 OpenAI 兼容接口（vLLM, Ollama 等）。

- 如果使用 DashScope，请设置 `DASHSCOPE_API_KEY` 环境变量。
- 对于本地模型，在智能体配置中设置 `model_server` 端点。

---

## 快速开发

以下示例展示了如何创建一个带有自定义工具的智能体：

```python
from agent_cascade.agents import Assistant
from agent_cascade.tools.base import BaseTool, register_tool

@register_tool('my_image_gen')
class MyImageGen(BaseTool):
    description = 'AI 绘画服务'
    parameters = [{'name': 'prompt', 'type': 'string', 'required': True}]

    def call(self, params: str, **kwargs) -> str:
        # 实现逻辑...
        return '{"image_url": "..."}'

llm_cfg = {'model': 'qwen-max-latest'}
bot = Assistant(llm=llm_cfg, function_list=['my_image_gen', 'code_interpreter'])

# 作为聊天机器人运行
for response in bot.run(messages=[{'role': 'user', 'content': '画一只狗'}]):
    print(response)
```

---

## FAQ

- **如何使用代码解释器？**：确保 Docker 已启动。该工具在隔离容器中编写并执行代码。
- **如何使用 MCP？**：在智能体配置中配置 `mcpServers`。详见 [MCP 示例](./examples/assistant_mcp_sqlite_bot.py)。
- **支持并行工具调用吗？**：支持，通过 `nous` 提示词模板原生支持。

---

## 致谢与来源

**AgentCascade** 最初 fork 自 [QwenLM/Qwen-Agent](https://github.com/QwenLM/Qwen-Agent)。我们非常感谢 Qwen 团队提供的强大基础，使得本框架能够在此之上不断演进。

---

## 免责声明

基于 Docker 的代码解释器仅用于本地测试。在生产环境中使用智能体执行代码时，请始终保持谨慎。
