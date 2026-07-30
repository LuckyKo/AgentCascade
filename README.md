# AgentCascade

AgentCascade is a professional multi-agent orchestration system designed to solve complex, multi-step problems that are too large for a single AI model to handle. By coordinating a pool of specialized agents, the system ensures that every part of a task is handled by the most qualified expert, resulting in production-grade outputs and reliable automation.

### Example Use Cases

- **Code review and refactoring**: Submit a pull request diff; an orchestrator delegates code analysis, security scanning, and style checks to specialized agents, then returns a consolidated report with actionable suggestions.
- **Research and synthesis**: Ask for a technical comparison across multiple sources; the system spawns research agents, extracts relevant sections, and produces a structured summary with citations.

## The Cascading Delegation Model

At the core of AgentCascade is a tree-like structure of agent interaction. Rather than following a simple linear chain, the system employs a hierarchical delegation process:

1. The lead orchestrator receives a high-level goal and decomposes it into smaller, manageable tasks.
2. Specialized sub-agents are called to handle these specific tasks.
3. If a sub-agent encounters a further complexity, it can delegate to its own sub-agents, creating a cascading tree of expertise.
4. Results flow back up the tree, being refined and synthesized at each level until a final, polished answer is delivered.

## Resilience and Reliability

AgentCascade is built to be robust and fault-tolerant, ensuring that complex workflows do not fail silently:

- Error Recovery: The system features automatic loop detection. If agents begin repeating themselves or get stuck in a logic cycle, the system identifies the pattern and rolls back to a previous stable state to try a different approach.
- Session Persistence: Every interaction is recorded in append-only logs. This allows for session resurrection, where a crashed or interrupted task can be resumed exactly where it left off.
- Adaptive Context: To maintain precision over long tasks, the system automatically compresses old conversation history, ensuring the agents always have the most relevant information without exceeding memory limits.

## Production Quality and Automated Security

Designed for professional environments, AgentCascade prioritizes the quality of the output and the safety of the host system:

- Production-Grade Output: By utilizing specialized agent types for different domains (such as mathematics, document analysis, or coding), the system avoids the generalist pitfalls of standard LLMs and delivers precise, expert-level results.
- Automated Security Advisor: A dedicated security layer reviews sensitive tool calls. If an agent attempts a potentially dangerous operation, a Security Advisor agent evaluates the risk and approves/rejects the operation, or advises the user on the safety of the operation if set on manual mode.
- Safe Execution: All code execution is isolated within Docker containers, ensuring that the agents can write and test code without ever risking the integrity of the host machine.

## Multi-Provider API Routing

AgentCascade supports multiple model providers simultaneously, with each agent type able to use its own priority-ordered list of endpoints for automatic failover. When an endpoint fails, requests seamlessly fall back to the next available provider in line.

Configuration is managed via `config/api_endpoints.json` or through the Web UI and REST API (`/api/endpoints`). The system uses a four-tier fallback strategy: agent-specific priorities → caller inheritance → last successful endpoint → global default. This allows you to assign fast, cost-effective models to simple tasks while reserving powerful models for complex orchestration work.

For example, a coder agent might prioritize a local model for speed, with a cloud provider as backup, while the orchestrator uses a premium model by default:

```json
{
  "agent_priorities": {
    "coder": ["local-llama", "openai-fallback"],
    "orchestrator": ["anthropic-primary", "openai-backup"]
  }
}
```

## Getting Started

### Prerequisites

- Python 3.10+
- Docker Engine 20.10+ (required for `code_interpreter` tool) ([installation docs](https://docs.docker.com/engine/install/))
- A running LLM API server. Options include:
  - Local: [LM Studio](https://lmstudio.ai/) (user-friendly), [llama-autoloader](https://github.com/LuckyKo/llama-autoloader) (optimized for AgentCascade), or llama.cpp
  - Cloud: OpenAI, Anthropic, DashScope, or any OpenAI-compatible API

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/LuckyKo/AgentCascade.git
   cd AgentCascade
   ```
2. Install the package in editable mode:
   ```bash
   pip install -e .
   ```
3. (Optional) Install extras for specific capabilities:
   - For document analysis: `pip install agent-cascade[rag]`
   - For code execution: `pip install agent-cascade[code_interpreter]`
   - For MCP support: `pip install agent-cascade[mcp]`

### Basic Usage

1. Configuration: Set the required environment variables for your chosen model provider. Example configuration:

   ```bash
   export QWEN_AGENT_MODEL=your-model-name
   export QWEN_AGENT_API_BASE=https://api.openai.com/v1
   export QWEN_AGENT_API_KEY=sk-your-api-key-here
   ```

2. Launch: Start the API server by running:
   ```bash
   python start_api_server.py
   ```
   By default, the server listens on port **12345**. To use a custom port: `python start_api_server.py --port <PORT>`
   
   To enable automatic security checks for all tool calls, add `--auto_security`:
   
3. Interact: Open the Web UI at `http://localhost:12345` (or your configured port) to send your first request. The lead orchestrator will automatically begin building the agent tree to solve your task.
   
### Programmatic Usage

You can interact with AgentCascade programmatically via WebSocket or REST endpoints.

**WebSocket (real-time chat):**

Connect to `ws://localhost:12345/ws/chat` using a WebSocket client.

**REST API (encrypted message flow):**

The system uses a handshake-based encrypted communication pattern:
- `/api/keys` — Retrieve public encryption key
- `/api/handshake` — Establish session
- `/api/message` — Send encrypted messages

For full endpoint details, request/response formats, and WebSocket protocol, see [docs/API_REFERENCE.md](docs/API_REFERENCE.md).

## Troubleshooting

- **Docker not running / container fails**: Ensure Docker is installed and the daemon is running. Check logs with `docker ps` and `docker logs <container_id>`. If using WSL2 on Windows, make sure the WSL distro is started and Docker Desktop is running.
- **Port already in use**: Port 12345 is used by default. Change it with `python start_api_server.py --port <PORT>` or stop the process occupying the port.
- **API key errors**: Verify that `QWEN_AGENT_API_KEY` (and any other provider-specific keys) is set correctly and has sufficient permissions/quota for the configured model.
- **Agent not responding**: Check server logs for errors. Common causes include network issues to the model API, timeouts due to large tasks, or insufficient context memory. You can increase timeouts in your environment configuration or split very large tasks into smaller sub-tasks.

## License

This project is licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.