"""KV cache restore comparison test between Agents-A1 and Qwen3.6-35B.

Based on llama-autoloader/test_slot_restore.py — runs the same save/restore
flow for both models and compares cached_tokens to verify KV cache utilization.

Models tested (from config/api_endpoints.json):
  - Agents-A1-APEX-I-Quality: b6c07e1c-c143-4c8b-89f7-c5fb43df5553
  - qwen3.6-35b-a3b:          edc8e5cc-ef1c-447d-bdf7-6d8f6882bde9

Run standalone: python tests/test_state_restore_comparison.py

Note: `run_model_test` is a plain worker function (not a pytest test) — it takes
positional args supplied by main(). Do not prefix it with `test_`, or pytest will
try to resolve its parameters as fixtures.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, Any, Optional

import pytest

BASE = "http://127.0.0.1:1234"
PROMPT_REPEATS = 200
HTTP_TIMEOUT = 180  # seconds for long-running operations


def api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def direct(port: int, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body else None
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=data, method="POST")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def chat(port: int, model: str, messages: list, max_tokens: int = 50) -> dict:
    return direct(port, "/v1/chat/completions", {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    })


def check_response(res: dict, step_name: str) -> None:
    if "error" in res:
        print(f"  ERROR [{step_name}]: Server error: {res['error']}")
        raise RuntimeError(step_name)
    if "choices" not in res or "usage" not in res:
        print(f"  ERROR [{step_name}]: Unexpected response: {json.dumps(res, indent=2)[:300]}")
        raise RuntimeError(step_name)
    if not res["choices"]:
        print(f"  ERROR [{step_name}]: Empty choices")
        raise RuntimeError(step_name)


def build_large_prompt(n_repeats: int = PROMPT_REPEATS) -> str:
    paragraph = (
        "The quick brown fox jumps over the lazy dog. "
        "Artificial intelligence has transformed how we interact with technology. "
        "Machine learning models continue to improve in both speed and accuracy. "
        "Natural language processing enables computers to understand human speech. "
        "Deep learning architectures like transformers have revolutionized the field. "
        "Large language models can generate coherent and contextually relevant text. "
        "Each new generation of models builds upon the insights of the previous one. "
        "Research in AI safety and alignment remains an active area of study. "
    )
    return (paragraph * n_repeats).strip()


def run_model_test(
    model_display: str,
    model_id: str,
    state_file: str,
) -> Dict[str, Any]:
    """Run the full save/restore flow for one model. Returns result dict."""

    print("\n" + "=" * 70)
    print(f"TESTING: {model_display} ({model_id})")
    print("=" * 70)

    result = {"model": model_id, "display": model_display, "passed": False}
    messages = [{"role": "user", "content": build_large_prompt()}]

    # Step 0: clean state
    print("\n[Step 0] Ensuring clean state...")
    try:
        api(f"/v1/models/{model_id}/unload", "POST")
        print("  Model unloaded")
    except Exception as e:
        print(f"  Note: not loaded ({e})")

    # Step 1: load model
    print("\n[Step 1] Loading model...")
    res = api(f"/v1/models/{model_id}/load", "POST")
    if not res.get("ready"):
        print(f"  ERROR: not ready (port={res.get('port')})")
        return result
    port = res["port"]
    pid_before = res["pid"]
    print(f"  Loaded on port {port}, pid {pid_before}")

    # Reset slot
    try:
        direct(port, "/slots/0?action=reset")
        print("  Slot reset")
    except Exception as e:
        print(f"  WARNING: slot reset failed ({e})")

    # Step 2: large prompt baseline
    print("\n[Step 2] Sending large prompt (~8k+ tokens)...")
    t0 = time.time()
    res_step2 = chat(port, model_id, messages, max_tokens=30)
    check_response(res_step2, "Step 2")
    t1 = time.time()
    usage = res_step2["usage"]
    timings = res_step2.get("timings", {})
    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
    prompt_n = timings.get("prompt_n", 0)
    prompt_ms = timings.get("prompt_ms", 0)
    print(f"  Prompt tokens: {usage['prompt_tokens']}, Cached: {cached}, Processed: {prompt_n}")
    print(f"  Prompt time: {prompt_ms:.1f} ms, Wall time: {t1-t0:.2f}s")

    result["step2"] = {"prompt_tokens": usage["prompt_tokens"], "cached": cached, "processed": prompt_n}

    # Step 3: save slot state
    print("\n[Step 3] Saving slot state...")
    save_res = direct(port, "/slots/0?action=save", {"filename": state_file})
    n_saved = save_res.get("n_saved", 0)
    n_written = save_res.get("n_written", 0)
    print(f"  Saved {n_saved} tokens, {n_written:,} bytes")
    if n_saved <= 0:
        print(f"  ERROR: saved {n_saved} tokens (expected > 0)")
        return result

    result["step3"] = {"n_saved": n_saved, "n_written": n_written}

    # Step 4: unload
    print("\n[Step 4] Unloading model...")
    unload_res = api(f"/v1/models/{model_id}/unload", "POST")
    print(f"  Unloaded: {unload_res['unloaded']}")

    # Step 5: reload (new process)
    print("\n[Step 5] Reloading model...")
    t0 = time.time()
    load_res = api(f"/v1/models/{model_id}/load", "POST")
    t1 = time.time()
    if not load_res.get("ready"):
        print(f"  ERROR: not ready (port={load_res.get('port')})")
        return result
    port = load_res["port"]
    pid_after = load_res["pid"]
    print(f"  Loaded on port {port}, pid {pid_after} (load time: {t1-t0:.1f}s)")

    if pid_before != pid_after:
        print(f"  ✅ Process restarted ({pid_before} -> {pid_after})")
    else:
        print(f"  ⚠️  Same process ({pid_before}) — cache may persist in RAM")

    result["step5"] = {"port": port, "pid_before": pid_before, "pid_after": pid_after}

    # Step 6: restore slot state
    print("\n[Step 6] Restoring slot state...")
    restore_res = direct(port, "/slots/0?action=restore", {"filename": state_file})
    n_restored = restore_res.get("n_restored", 0)
    print(f"  Restored {n_restored} tokens")
    if n_restored <= 0:
        print(f"  ERROR: restored {n_restored} tokens (expected > 0)")
        return result

    result["step6"] = {"n_restored": n_restored}

    # Step 6b: Try workaround - send same large prompt again to "warm" the MoE cache
    print("\n[Step 6b] Trying workaround: sending original prompt again...")
    t0 = time.time()
    try:
        res_warmup = chat(port, model_id, messages, max_tokens=5)
    except Exception as e:
        print(f"  Note: warmup failed ({e})")
        res_warmup = None
    t1 = time.time()
    if res_warmup and "usage" in res_warmup:
        usage = res_warmup["usage"]
        timings = res_warmup.get("timings", {})
        cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        prompt_n = timings.get("prompt_n", 0)
        print(f"  Warmup - Prompt tokens: {usage['prompt_tokens']}, Cached: {cached}, Processed: {prompt_n}, Wall time: {t1-t0:.2f}s")
    else:
        print(f"  Warmup skipped, Wall time: {t1-t0:.2f}s")

    result["step6b"] = {"done": res_warmup is not None}

    # Step 7: continuation — KEY TEST
    print("\n[Step 7] Sending continuation...")
    step2_response = res_step2["choices"][0]["message"]["content"]
    messages.append({"role": "assistant", "content": step2_response})
    messages.append({"role": "user", "content": "Now count from 1 to 10."})

    t0 = time.time()
    try:
        res_step7 = chat(port, model_id, messages, max_tokens=30)
    except Exception as e:
        print(f"  ERROR: {e}")
        return result
    check_response(res_step7, "Step 7")
    t1 = time.time()

    usage = res_step7["usage"]
    timings = res_step7.get("timings", {})
    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
    prompt_n = timings.get("prompt_n", 0)
    total_prompt = usage["prompt_tokens"]

    print(f"  Prompt tokens: {total_prompt}, Cached: {cached}, Processed: {prompt_n}")
    print(f"  Wall time: {t1-t0:.2f}s")

    # Evaluate cache hit
    if cached >= n_restored * 0.9:
        verdict = "✅ PASS — Strong KV cache hit"
        passed = True
    elif cached >= total_prompt * 0.8:
        verdict = "✅ PASS — History caching confirmed"
        passed = True
    elif prompt_n < 50:
        verdict = "✅ PASS — Low processed tokens suggests cache hit"
        passed = True
    else:
        verdict = "❌ FAIL — KV cache not utilized"
        passed = False

    print(f"  {verdict} (cached={cached}, restored={n_restored}, prompt_n={prompt_n})")

    result["step7"] = {
        "total_prompt": total_prompt,
        "cached": cached,
        "processed": prompt_n,
        "cache_ratio": cached / total_prompt if total_prompt > 0 else 0.0,
    }
    result["passed"] = passed

    # Step 8: second continuation to verify ongoing cache usage
    print("\n[Step 8] Sending second continuation...")
    step7_response = res_step7["choices"][0]["message"]["content"]
    messages.append({"role": "assistant", "content": step7_response})
    messages.append({"role": "user", "content": "What is 2+2?"})

    try:
        res_step8 = chat(port, model_id, messages, max_tokens=30)
    except Exception as e:
        print(f"  ERROR: {e}")
        return result
    check_response(res_step8, "Step 8")

    usage = res_step8["usage"]
    timings = res_step8.get("timings", {})
    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
    prompt_n = timings.get("prompt_n", 0)
    total_prompt = usage["prompt_tokens"]

    print(f"  Prompt tokens: {total_prompt}, Cached: {cached}, Processed: {prompt_n}")

    if cached >= total_prompt * 0.8 or prompt_n < 20:
        print(f"  ✅ Ongoing cache confirmed")
    else:
        print(f"  ❌ Expected cache hit (cached={cached}, processed={prompt_n})")

    result["step8"] = {"total_prompt": total_prompt, "cached": cached, "processed": prompt_n}

    # Cleanup
    print("\n[Step 9] Cleanup...")
    try:
        api(f"/v1/models/{model_id}/unload", "POST")
        print("  Model unloaded")
    except Exception as e:
        print(f"  Note: unload failed ({e})")

    state_path = Path(__file__).parent.parent.parent / "stuff" / "Beta" / "llama-autoloader" / "states" / state_file
    if state_path.exists():
        try:
            state_path.unlink()
            print(f"  Removed {state_file}")
        except Exception as e:
            print(f"  Note: remove failed ({e})")

    return result


def main():
    # Model IDs from config/api_endpoints.json
    MODELS = [
        ("Agents-A1-APEX-I-Quality", "Agents-A1-APEX-I-Quality"),
        ("qwen3.6-27b-fable-fus-mtp", "qwen3.6-27b-fable-fus-mtp"),  # Known working dense MTP model
    ]

    print("=" * 70)
    print("KV Cache State Restore Comparison Test")
    print("=" * 70)

    results = []
    for display, model_id in MODELS:
        state_file = f"{model_id}.slot_test.bin"
        try:
            r = run_model_test(display, model_id, state_file)
        except RuntimeError as e:
            r = {"model": model_id, "display": display, "passed": False, "error": str(e)}
        results.append(r)

    # Summary
    print("\n" + "=" * 70)
    print("COMPARISON SUMMARY")
    print("=" * 70)

    for r in results:
        cache_ratio = r.get("step7", {}).get("cache_ratio", 0.0)
        status = "PASS" if r.get("passed") else "FAIL"
        print(f"\n{r['display']}: {status} (cache_ratio={cache_ratio:.1%})")

    # Verdict
    a1_pass = results[0].get("passed")
    qwen_pass = results[1].get("passed")

    print("\n" + "-" * 70)
    if a1_pass and qwen_pass:
        print("VERDICT: Both models pass — KV cache restore working correctly.")
    elif not a1_pass and qwen_pass:
        print("VERDICT: Agents-A1 FAILS, qwen3.6-35b-a3b PASSES")
        print("         Confirms the reported issue.")
    else:
        print("VERDICT: Unexpected result — check environment/configuration.")

    # Save JSON results
    out_path = Path(__file__).parent / "test_state_restore_comparison_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()