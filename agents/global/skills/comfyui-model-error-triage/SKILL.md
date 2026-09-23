---
name: comfyui-model-error-triage
description: Diagnose ComfyUI model-load / sampling errors (shape mismatches, "Could not detect model type", gguf loader failures) down to root cause using the log + source.
source: auto-generated
version: "1.0.0"
triggers:
  - "comfyui error"
  - "comfyui log"
  - "gguf loader"
  - "could not detect model type"
  - "shape mismatch"
  - "normalized_shape"
  - "model load fails"
  - "diffusion model error"
generated_by: orchestrator
generated_from_task: "Check the comfyUI log and tell me what am I missing (gguf Qwen-Image-2.1 errors out)"
---

## Goal
Turn a ComfyUI crash into a precise root cause (which file, which dim, why) instead of guessing, by reading the log traceback and cross-checking it against ComfyUI's detection + model code.

## Procedure

### Step 1 — Find and read the live log
- Portable Windows install: `ComfyUI/user/comfyui.log` (current), `comfyui.prev.log`, `comfyui.prev2.log`. Also `logs/workers/*.log`.
- Read the TAIL first (`read_file` with a high `start_line`). The crash block starts at a line like `!!! Exception during processing !!! <one-line error>` and is followed by the full Python traceback.
- The one-line error + the DEEPEST frame in the traceback (the `.py` under `comfy/`, not `torch/`) tells you which module and which op failed.

### Step 2 — Interpret shape errors
- `RuntimeError: Given normalized_shape=[N], expected input with shape [*N], but got input of size[1, L, M]` → a norm/projection layer expects feature dim **N** but the incoming tensor has last dim **M**. One side (the weight) is wrong for the other (the runtime data).
- Work out which is "correct": the runtime value usually comes from a well-known upstream model. Verify the *expected* dim against the real model's config (e.g. pull `config.json` from HuggingFace and read `text_config.hidden_size`) rather than trusting the loaded weight.

### Step 3 — Trace how ComfyUI built that dim
- Model type + dims are inferred in `comfy/model_detection.py` (`detect_unet_config`, `model_config_from_unet`). It reads shapes off state-dict keys, e.g. `context_in_dim = txt_in.text_norm.weight.shape[0]`. So a wrong weight shape silently produces a wrong model config — no error at load time.
- The actual module is under `comfy/ldm/<model>/model.py`; the text encoder under `comfy/text_encoders/<model>.py` (often reuses a base like `qwen3vl.py` / `llama.py` dataclasses that hold the true `hidden_size`).

### Step 4 — gguf-specific gotcha
- The third-party gguf loader (`custom_nodes/gguf/pig.py`) loads weights opaquely via `load_gguf_sd` and calls `comfy.sd.load_diffusion_model_state_dict`. It does NOT cross-check loaded weight shapes against the text encoder's output dim, so a mismatched gguf builds a model that only crashes on the first forward pass (mid-sampling), not at load.
- If a shape error appears in a gguf-loaded DiT's `txt_in`/text branch, suspect the **DiT gguf** was packed against a different encoder/architecture revision than the TE you paired it with.

### Step 5 — State the fix
- Name the exact file + tensor key whose shape is wrong, and what it should be. Usually: re-download the official model (or a matching repack) so DiT and TE are the same revision. Confirm VAE/TE/DiT all come from one release.

## Tips
- Don't trust `model_type` printed in the log alone — confirm by reading the detection code path for that specific model.
- A "latest ComfyUI + latest gguf loader" does NOT guarantee a brand-new model is wired up; check the build's `comfyui_version.py`. New models often have fresh, untested detection paths.
- Verify upstream dims from authoritative config (HF `config.json`), never from the possibly-wrong loaded weight.
- Distinguish "wrong file" (re-download fixes it) from "loader bug" (needs a code fix). A shape mismatch between DiT and TE is almost always the wrong file.
