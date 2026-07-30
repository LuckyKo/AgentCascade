"""Shared utilities for inner loop detector tests.

Provides log discovery, text extraction, and detector loading helpers
used by both streaming simulation tests and live data tests.
"""

import json
import random
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths & imports — load the detector directly to avoid pulling in the full
# agent_cascade package (which has pydantic / tiktoken dependencies).
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import importlib.util as _util

_settings_spec = _util.spec_from_file_location(
    "settings",
    PROJECT_ROOT / "agent_cascade" / "settings.py",
)
_settings_mod = _util.module_from_spec(_settings_spec)
sys.modules["agent_cascade.settings"] = _settings_mod
_settings_spec.loader.exec_module(_settings_mod)

_spec = _util.spec_from_file_location(
    "inner_loop_detect",
    PROJECT_ROOT / "agent_cascade" / "inner_loop_detect.py",
)
_mod = _util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

InnerLoopDetector = _mod.InnerLoopDetector
InnerLoopSettings = _settings_mod.InnerLoopSettings


# ---------------------------------------------------------------------------
# Log discovery
# ---------------------------------------------------------------------------

def find_log_dir() -> Optional[Path]:
    """Return the first existing log directory, or None.
    
    Checks multiple candidate directories to work both on the host
    (N:/work/...) and inside Docker containers (/workspace/logs).
    """
    candidates = [
        Path("/workspace/logs"),
        PROJECT_ROOT.parent / "logs",
        Path(r"N:\work\WD\AgentWorkspace\logs"),
    ]
    for p in candidates:
        if p.is_dir():
            return p
    return None


LOG_DIR = find_log_dir()


# ---------------------------------------------------------------------------
# Text extraction from logs
# ---------------------------------------------------------------------------

def extract_assistant_texts(log_dir: Path, min_length: int = 200) -> list[str]:
    """Extract combined reasoning_content + content from every assistant message.
    
    Handles both nested format {"message": {...}} and top-level format
    {"role": "assistant", ...} found in AgentCascade log files.
    """
    texts: list[str] = []
    for fname in sorted(log_dir.iterdir()):
        if fname.suffix != ".jsonl":
            continue
        with open(fname, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                # Nested message format: {"message": {...}}
                msg = entry.get("message", entry.get("msg"))
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    content = msg.get("content") or ""
                    reasoning = msg.get("reasoning_content", msg.get("reasoning")) or ""
                    full_text = reasoning + content
                    if len(full_text) >= min_length:
                        texts.append(full_text.strip())
                    continue

                # Top-level format: {"role": "assistant", ...}
                if entry.get("role") == "assistant":
                    content = entry.get("content") or ""
                    reasoning = entry.get("reasoning_content", entry.get("reasoning")) or ""
                    full_text = reasoning + content
                    if len(full_text) >= min_length:
                        texts.append(full_text.strip())

    return texts


# ---------------------------------------------------------------------------
# Cached text accessor for tests
# ---------------------------------------------------------------------------

def get_assistant_texts(min_length: int = 200) -> list[str]:
    """Return assistant texts, cached across test calls for speed."""
    if not hasattr(get_assistant_texts, "_cache"):
        get_assistant_texts._cache = extract_assistant_texts(LOG_DIR, min_length) if LOG_DIR else []
    return get_assistant_texts._cache


# ---------------------------------------------------------------------------
# Synthetic test helpers
# ---------------------------------------------------------------------------

def make_unique_filler(min_chars: int = 4500) -> str:
    """Generate unique non-repetitive text for synthetic tests.
    
    Uses varied sentence templates with numeric suffixes so no fragments
    overlap when chunked at small sizes. Result exceeds min_chars without
    triggering any repetition detection mode.
    """
    parts = []
    i = 0
    while len(" ".join(parts)) < min_chars:
        parts.append(f"Step {i} involves examining component alpha-{i} for correctness and completeness.")
        parts.append(f"Then I verify that module beta-{i + 100} handles edge cases properly too.")
        parts.append(f"Finally checking subsystem gamma-{i + 200} against the reference implementation spec.")
        i += 1
    return " ".join(parts)


def feed_streaming(
    text: str,
    chunk_size_strategy: str = "fixed",
    base_chunk_size: int = 20,
) -> Optional[dict]:
    """Feed text through the detector using realistic streaming chunks.
    
    Args:
        text: Full text to feed (e.g., an assistant response).
        chunk_size_strategy: "fixed" for uniform chunks, "random" for variable sizes.
        base_chunk_size: For "fixed", exact chunk size. For "random", midpoint of range.
    
    Returns:
        Detection result dict if loop detected, None otherwise.
    """
    settings = InnerLoopSettings()
    detector = InnerLoopDetector(settings=settings)

    rng = random.Random(42)  # Fixed seed for reproducibility
    pos = 0

    while pos < len(text):
        if chunk_size_strategy == "random":
            actual_chunk = rng.randint(10, 40)
        else:
            actual_chunk = base_chunk_size

        chunk = text[pos : pos + actual_chunk]
        result = detector.feed(chunk)
        if result:
            return result
        pos += actual_chunk

    return None