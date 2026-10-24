---
version: 1.1.0
name: yue2-music
description: Generate songs with YuE2 via audio.cpp (dev branch) on Windows/CUDA, safely swapping VRAM with the llama-autoloader LLM that serves local AC agents. Use for text/lyrics-to-song generation, batch album runs, VRAM-free orchestration, model-quant selection (bf16/q8_0), temperature control, and reproducing runs.
triggers:
  - yue2
  - yue music
  - generate song
  - make a song
  - lyrics to music
  - audiocpp_cli gen
  - deep house track
---

## Yue2 Music Generator Use
Turn a style + lyrics request into a reproducible `.wav` using YuE2 in **audio.cpp** (not the research `YuE` Python repo), on a dual-GPU Windows box where the same GPUs also run the llama-autoloader LLM that powers local AC agents — without VRAM contention.

## Environment facts (this machine, verified 2026-09-15 / 2026-09-17 / 2026-10-02)
- audio.cpp must be built from the **`dev` branch** (YuE2 does not exist on `main`). Binary: `N:\work\WD\audio.cpp\build\windows-cuda-release\bin\audiocpp_cli.exe`.
- Model dir: `N:\work\stuff\Beta\audio\Yue2` — needs the main-model GGUF **plus** a `sidecars/` folder (`yue2-model-config.json`, `yue2-generation-config.json`, `yue2-qwen.tiktoken`, `yue2-vae-config.json`). Sidecar source: `https://huggingface.co/audio-cpp/Yue2-3B-GGUF/resolve/main/sidecars/<file>`.
- **Two main-model quants are present locally** (select via `--model-gguf` / `yue2.model_gguf=`):
  - `yue2-3b-bf16.gguf` — ~6.8 GB, higher fidelity. **Now the script default.**
  - `yue2-3b-q8_0.gguf` — ~4.0 GB, lighter/faster, very close quality for forgiving genres. (This is also the *CLI's own* built-in default if you pass no override.)
- The local VAE is **f32**, so every run needs `--session-option yue2.vae_gguf=yue2-vae-f32.gguf` (the default expects f16 and will error). Component GGUF paths are **relative to `--model`**; absolute paths are rejected by the CLI.
- GPUs: 2× RTX 5060 Ti (sm_120, CUDA 13.1). Output is 48 kHz stereo 16-bit PCM (~2–3.5 min per track at `num_inference_steps=32`). Both quants fit comfortably on one GPU with the loader swapped out.
- The llama-autoloader LLM (e.g. Qwen3.8-27B) runs on `http://127.0.0.1:1234` and shares the GPUs. **It auto-reloads a model on demand** after an unload — you do NOT need to manually reload it.

## Procedure
### Step 0 — Ready-to-run presets (fastest path)
Two editable `.bat` wrappers live in the workspace, both pre-wired with the quirky-deep-house example and every knob exposed as a top-of-file variable:
- **`N:\work\WD\AgentWorkspace\gen_song.bat`** → bf16 default (`MODEL_GGUF=yue2-3b-bf16.gguf`).
- **`N:\work\WD\AgentWorkspace\gen_song_q8.bat`** → lighter q8_0 (`MODEL_GGUF=yue2-3b-q8_0.gguf`).

Edit the `set "..."` lines (LYRICS, STYLE, OUT, SEED, TEMP, COT, CFG_SCALE, STEPS, DEVICE) and double-click / run. They echo the resolved settings before launching. Use these for everyday runs; drop to the Python scripts or raw CLI only when you need something they don't expose.

### Step 1 — Pick the right wrapper (if not using the .bat presets)
- **`generate_song.py`** (full swap): check loader → find loaded model → **save KV state** → `unload_all` → poll nvidia-smi until free → run YuE2 → **restore model + KV**. Best when you want the agent to resume with *zero* reprocessing.
- **`generate_song_nokv.py --no-kv`** (lean swap): check loader → find loaded model → `unload_all` → poll until free → run YuE2. **No KV save/restore and no manual reload** — the llama-autoloader rehydrates the model on demand and AC rebuilds its session from append-only JSONL logs. Simpler, fewer security-agent flags, but the agent reprocesses context on resume. Prefer this for batch runs where you don't care about preserving KV.

```powershell
python N:\work\WD\AgentWorkspace\generate_song_nokv.py --no-kv `
  --lyrics "@path/to/lyrics.txt" `
  --style "English, indie pop, bright acoustic guitar, soft drums, warm lead vocal" `
  --out "N:\work\WD\AgentWorkspace\song.wav" `
  --seed 831001 --device 0
```

### Step 2 — CRITICAL: how to run it without killing your own agent
The active AC agent is often backed by the very llama-server the script unloads. Two safe patterns (both verified):
1. **Single blocking sync shell command, no heartbeats.** Run the generation as ONE `shell_cmd` with `execution_mode=sync`. The process holds your turn until it returns, so no heartbeat ever pings the loader mid-swap. This is the safest and simplest — use it for single tracks or sequential batches (chain with `&&`). **Note:** a plain `shell_cmd` defaults to auto mode and will *background* anything over ~60s; you must pass `execution_mode=sync` explicitly to force foreground.
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
  --session-option yue2.model_gguf=yue2-3b-bf16.gguf `
  --session-option yue2.vae_gguf=yue2-vae-f32.gguf `
  --lyrics "[Verse]...[Chorus]..." `
  --request-option style="English, indie pop, ..." `
  --request-option cot=off `
  --request-option semantic_temperature=1.15 `
  --seed 831001 `
  --out "N:\work\WD\AgentWorkspace\song.wav" --log
```

### Step 5 — Judge success by the file, not the exit code
The CLI writes TIMING/TRACE logs to **stderr**; PowerShell reports exit code 1 even on success. Verify a non-empty `.wav` exists and check `session.wall_ms` in the log. Validate the header (expect PCM fmt=1, 2 ch, 48000 Hz, 16-bit). After a batch, confirm the loader is healthy: `curl -s http://127.0.0.1:1234/v1/health` → `{"status":"ok","models_loaded":1}`.

## generate_song.py flags (verified 2026-10-02)
Beyond the original set, the script now exposes:
- **`--model-gguf <file>`** — main AR/NAR quant, relative to `--model` dir. Default `yue2-3b-bf16.gguf` (override env `YUE2_MODEL_GGUF`). Pass `yue2-3b-q8_0.gguf` for the lighter build.
- **`--temp <float>`** — sampling temperature, default `1.0`. Maps to `semantic_temperature` (always active) and, when `cot=melody|full`, also to `abc_temperature`. See "Temperature guidance" below.
- Unchanged: `--lyrics` (inline or `@file.txt`), `--style`, `--out`, `--seed`, `--device`, `--threads`, `--cot off|melody|full`, `--cfg-scale`, `--steps`, `--abc`, `--abc-file`, `--state-label`, `--no-swap`.

## Temperature guidance (verified 2026-10-02)
The paper (arXiv YuE2) and ComfyUI guide do **not** publish a "max temp"; the model's tested/safe operating point is the audio.cpp default `semantic_temperature=1.0`.
- **1.0** = baseline, most coherent.
- **~1.1–1.2** = gentle lift for more variety while staying coherent — good for "make it quirkier/wilder" requests.
- **> ~1.2** = risk of incoherent semantic tokens → garbled vocals or broken song structure. Avoid unless you specifically want chaos and accept the risk.
- Changing `--temp` alone does NOT change where the model writes its end token; duration also varies with seed/quant (see "Duration is budget-based").

## Duration is budget-based
YuE2 samples at **25 semantic tokens/sec** of requested duration and stops as soon as it emits the song's end token — so a track can end well before any nominal max. Same lyrics + same seed can still yield different lengths across quants (bf16 vs q8_0) or temps, because the model decides when to stop. This is expected, not a bug.

## Writing good lyrics for YuE2 (from official ComfyUI guide + skill experience)
- **Use section tags** to drive structure: `[Verse]`, `[Pre-Chorus]`, `[Chorus]`, `[Bridge]`, `[Outro]`. Repeat the `[Chorus]` block where you want it to recur.
- **Only sung words in the lyrics field.** Put ALL musical direction (genre, BPM, instruments, mood, vocal type) in the `--style` prompt, never in the lyric body.
- **Keep lines reasonably close in length/syllable count** — helps phrasing; matters most for covers but generally improves results.
- **Longer prompts shorten the song**: style + lyrics + ABC share one context budget, so an over-long style/lyric block reduces the music budget (or fails with "no room for music"). Keep style prompts focused.

## Model-quant selection: bf16 vs q8_0
- **bf16** (`yue2-3b-bf16.gguf`, ~6.8 GB): highest fidelity; slightly cleaner vocals/timbres on sustained pads and leads. Default for quality-first runs.
- **q8_0** (`yue2-3b-q8_0.gguf`, ~4.0 GB): lighter, faster (~109 s vs ~140 s CLI on a 5060 Ti), leaves more VRAM headroom (can sometimes run without a full swap, or batch longer). Very close in quality for forgiving genres like deep house; may show faint quantization artifacts on delicate sustained notes.
- Both are driven identically — only `--model-gguf` differs. For an A/B, hold lyrics/style/temp/seed constant and change only the quant.

## Known bug (fixed in generate_song_nokv.py)
`find_loaded_model()` had a fallback loop `for m in body:` that iterated the response **dict's string keys** (`"object"`, `"data"`) and called `.get()` on them → `AttributeError: 'str' object has no attribute 'get'`. This triggered when NO model was loaded (e.g. right after a prior run left the model unloaded). Fix: iterate the `models` (the `data` list) in both loops and guard `isinstance(m, dict)`. If you hit this on the original `generate_song.py`, apply the same fix or use the `--no-kv` variant.

## Tips
- Keep a reproducible record per run: exact style, lyrics, seed, cot, cfg_scale, steps, **model_gguf**, **temp**, output path (write an album README).
- Do NOT silently lower `--steps` or drop quality to hide an OOM — free VRAM (swap the loader) or report the constraint.
- "It ran and produced audio" ≠ "it's musically good." Listen before declaring success.
- YuE2 has no audio-reference / phoneme-alignment / inpainting argument; it conditions on style + lyrics (+ optional ABC). For melody-preserving work use `cot=melody` or `cot=full` with an ABC file (see the research `YuE` repo skill for that deeper workflow — different codebase).
- If VRAM never frees after unload, WDDM can hold reservations briefly; the script polls up to 180 s before proceeding.
- For a concept album, give each track a DISTINCT style prompt so the album varies instead of repeating one sound.
- When you change only the seed (same lyrics/style), you get a new take on the same song; when you want genuinely wilder output, raise `--temp` toward 1.2 rather than just re-seeding.
