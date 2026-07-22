FROM python:3.12.12-slim

RUN apt-get update && apt-get install -y \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
    requests \
    ipykernel \
    jupyter_client \
    matplotlib \
    numpy \
    pandas \
    Pillow \
    seaborn \
    sympy \
    openpyxl \
    pyyaml \
    "pydantic>=2.3.0" \
    tiktoken \
    regex \
    json5 \
    jsonlines \
    jsonschema \
    python-dotenv \
    openai \
    "dashscope>=1.11.0" \
    aiohttp \
    eval_type_backport

# fix font issue in matplotlib
COPY AlibabaPuHuiTi-3-45-Light.ttf /usr/share/fonts/truetype/
RUN fc-cache -fv

RUN python -c "import matplotlib.pyplot as plt; import matplotlib.font_manager as fm; fm._load_fontmanager(try_read_cache=False)"