# Installation

- Install the stable version from PyPI:
```bash
pip install -U "agent-cascade[gui,rag,code_interpreter,mcp]"
# Or use `pip install -U agent-cascade` for the minimal requirements.
# The optional requirements, specified in double brackets, are:
#   [gui] for Gradio-based GUI support;
#   [rag] for RAG support;
#   [code_interpreter] for Code Interpreter support;
#   [mcp] for MCP support.
```

- Alternatively, you can install the latest development version from the source:
```bash
git clone https://github.com/QwenLM/AgentCascade.git
cd AgentCascade
pip install -e ./"[gui,rag,code_interpreter,mcp]"
# Or `pip install -e ./` for minimal requirements.
```
