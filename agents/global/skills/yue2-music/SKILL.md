---
version: 1.0.1
name: yue2-music
description: Generate songs with YuE2 via audio.cpp (dev branch) on Windows/CUDA, safely swapping VRAM with the llama-autoloader LLM that serves local AC agents. Use for text/lyrics-to-song generation, batch album runs, VRAM-free orchestration, and reproducing runs.
---

## Yue2 Music Generator Use
Turn a style + lyrics request into a reproducible `.wav` using YuE2 in **audio.cpp** (not the research `YuE` Python repo), on a dual-GPU Windows box where the same GPUs also run the llama-autoloader LLM that powers local AC agents — without VRAM contention.

## Environment facts (this machine, verified 2026-09-15 / 2026-09-17)
- audio.cpp must be built from the **`dev` branch** (YuE2 does not exist on `main`). Binary: `N:\work\WD\audio.cpp\build\windows-cuda-release\bin\audiocpp_cli.exe`.
- Model dir: `N:\work\stuff\Beta\audio\Yue2` — needs the 2 GGUFs **plus** a `sidecars/` folder (`yue2-model-config.json`, `yue2-generation-config.json`, `yue2-qwen.tiktoken`, `yue2-vae-config.json`). Sidecar source: `https://huggingface.co/audio-cpp/Yue2-3B-GGUF/resolve/main/sidecars/<file>`.
- The local VAE is **f32**, so every run needs `--session-option yue2.vae_gguf=yue2-vae-f32.gguf` (the default expects f16 and will error).
- GPUs: 2× RTX 5060 Ti (sm_120, CUDA 13.1). Output is 48 kHz stereo 16-bit PCM (~2–3.5 min per track at `num_inference_steps=32`).
- The llama-autoloader LLM (e.g. Qwen3.8-27B) runs on `http://127.0.0.1:1234` and shares the GPUs. **It auto-reloads a model on demand** after an unload — you do NOT need to manually reload it.

## Procedure
### Step 1 — Pick the right wrapper
- **`generate_song.py`** (full swap): check loader → find loaded model → **save KV state** → `unload_all` → poll nvidia-smi until free → run YuE2 → **restore model + KV**. Best when you want the agent to resume with *zero* reprocessing.
- **`generate_song_nokv.py --no-kv`** (lean swap): check loader → find loaded model → `unload_all` → poll until free → run YuE2. **No KV save/restore and no manual reload** — the llama-autoloader rehydrates the model on demand and AC rebuilds its session from append-only JSONL logs. Simpler, fewer security-agent flags, but the agent reprocesses context on resume. Prefer this for batch runs where you don't care about preserving KV.

```powershell
python N:\work\WD\AgentWorkspace\generate_song_nokv.py --no-kv `
  --lyrics "@path/to/lyrics.txt" `
  --style "English, indie pop, bright acoustic guitar, soft drums, warm lead vocal" `
  --out "N:\work\WD\AgentWorkspace\song.wav" `
  --seed 831001 --device 0
```
`--lyrics` accepts inline text or `@file.txt`. Optional: `--cot off|melody|full`, `--cfg-scale`, `--steps`, `--abc-file`, `--state-label`.

### Step 2 — CRITICAL: how to run it without killing your own agent
The active AC agent is often backed by the very llama-server the script unloads. Two safe patterns (both verified):
1. **Single blocking sync shell command, no heartbeats.** Run the generation as ONE `shell_cmd` with `execution_mode=sync`. The process holds your turn until it returns, so no heartbeat ever pings the loader mid-swap. This is the safest and simplest — use it for single tracks or sequential batches (chain with `&&`).
2. **Async + `heartbeat_interval=-1`** only if you truly need concurrency AND you can guarantee the swap sections don't interleave (see Step 3). Riskier; prefer sync.

### Step 3 — Parallelism: what actually works on this Windows box
- **cmd.exe has NO backgrounding/`wait`.** `A & B & wait` does NOT run in parallel — `&` just chains sequentially and `wait` is not a builtin (returns "not recognized"). Do not rely on it.
- The two scripts share ONE llama-autoloader + the same default state label (`yue2-swap`). Their `save/unload/restore` calls **race and clobber each other's session** if they interleave → exit 7 = lost session. So you cannot safely run two full-swap generations at once on the shared loader.
- Splitting `--device 0` / `--device 1` only isolates the YuE2 *generation* VRAM, NOT the shared loader control plane. It does not make parallel swaps safe.
- **Practical rule: run batch album tracks SEQUENTIALLY** (one sync command chaining `&&`, or separate sync calls). ~2–3 min/track; a 12-track album ≈ 30–40 min. True two-at-once generation is only worth it if you can serialize the loader swap (e.g. a lock) — not worth the complexity for most runs.
- PowerShell `Start-Job` *can* run two processes concurrently, but with the shared-loader race above it's unsafe unless you add explicit serialization of the unload/restore section.

### Step 4 — Manual CLI (if not using a script)
```powershell
cd N:\work\WD\audio.cpp\build\windows-cuda-release\bin
.\audiocpp_cli.exe --task gen --family yue2 `
  --model "N:\work\stuff\Beta\audio\Yue2" `
  --backend cuda --device 0 --threads 8 `
  --session-option yue2.vae_gguf=yue2-vae-f32.gguf `
  --lyrics "[Verse]...[Chorus]..." `
  --request-option style="English, indie pop, ..." `
  --request-option cot=off --seed 831001 `
  --out "N:\work\WD\AgentWorkspace\song.wav" --log
```

### Step 5 — Judge success by the file, not the exit code
The CLI writes TIMING/TRACE logs to **stderr**; PowerShell reports exit code 1 even on success. Verify a non-empty `.wav` exists and check `session.wall_ms` in the log. Validate the header (expect PCM fmt=1, 2 ch, 48000 Hz, 16-bit). After a batch, confirm the loader is healthy: `curl -s http://127.0.0.1:1234/v1/health` → `{"status":"ok","models_loaded":1}`.

## Known bug (fixed in generate_song_nokv.py)
`find_loaded_model()` had a fallback loop `for m in body:` that iterated the response **dict's string keys** (`"object"`, `"data"`) and called `.get()` on them → `AttributeError: 'str' object has no attribute 'get'`. This triggered when NO model was loaded (e.g. right after a prior run left the model unloaded). Fix: iterate the `models` (the `data` list) in both loops and guard `isinstance(m, dict)`. If you hit this on the original `generate_song.py`, apply the same fix or use `--no-kv` variant.

## Tips
- Keep a reproducible record per run: exact style, lyrics, seed, cot, cfg_scale, steps, output path (write an album README).
- Do NOT silently lower `--steps` or drop quality to hide an OOM — free VRAM (swap the loader) or report the constraint.
- "It ran and produced audio" ≠ "it's musically good." Listen before declaring success.
- YuE2 has no audio-reference / phoneme-alignment / inpainting argument; it conditions on style + lyrics (+ optional ABC). For melody-preserving work use `cot=melody` or `cot=full` with an ABC file (see the research `YuE` repo skill for that deeper workflow — different codebase).
- If VRAM never frees after unload, WDDM can hold reservations briefly; the script polls up to 180 s before proceeding.
- For a concept album, give each track a DISTINCT style prompt so the album varies instead of repeating one sound.
