"""
Profile script for AgentCascade streaming performance.

Usage:
    python profile_streaming.py
    
Then in another terminal, profile the running process:
    py-spy top --pid <PID>
    py-spy record -o profile.svg --pid <PID> --duration 30
"""
import os
import sys
import time
import json
from pathlib import Path

cascade_dir = Path(r"N:\work\WD\AgentCascade")
if str(cascade_dir) not in sys.path:
    sys.path.insert(0, str(cascade_dir))

print(f"[{time.strftime('%X')}] Starting profiler...")
print(f"[{time.strftime('%X')}] Current PID: {os.getpid()}")
print()

# Check for API key
api_key = os.getenv('DASHSCOPE_API_KEY', '') or os.getenv('OPENAI_API_KEY', '')
if not api_key:
    print(f"[{time.strftime('%X')}] ERROR: No API key found. Set DASHSCOPE_API_KEY or OPENAI_API_KEY")
    sys.exit(1)

print(f"[{time.strftime('%X')}] Using model: qwen3-235b-a22b (DashScope)")
print()

def profile_streaming():
    from agent_cascade.agents import Assistant
    
    # Create a simple agent with streaming
    bot = Assistant(
        llm={
            'model': 'qwen3-235b-a22b',
            'model_type': 'qwen_dashscope',
        },
        function_list=[],  # No tools for clean streaming test
        name='StreamProfiler'
    )
    
    messages = [{'role': 'user', 'content': 'Write a Python function that calculates fibonacci numbers up to n. Include type hints and docstrings.'}]
    
    print(f"[{time.strftime('%X')}] Starting streaming run...")
    print(f"[{time.strftime('%X')}] PID: {os.getpid()} - You can now profile with:")
    print(f"[{time.strftime('%X')}]   py-spy top --pid {os.getpid()}")
    print(f"[{time.strftime('%X')}]   py-spy record -o profile.svg --pid {os.getpid()} --duration 30")
    print()
    
    start_time = time.perf_counter()
    yield_count = 0
    timing_data = []
    msg_lengths = []
    
    try:
        for partial in bot.run(messages=messages):
            yield_count += 1
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            msg_count = len(partial)
            
            # Record timing every 5 yields to see growth pattern
            if yield_count % 5 == 0:
                timing_data.append({
                    'yield': yield_count,
                    'elapsed_ms': round(elapsed_ms, 1),
                    'msg_count': msg_count
                })
                print(f"[{time.strftime('%X')}] Yield {yield_count:4d} | {elapsed_ms:8.1f}ms elapsed | {msg_count:3d} messages")
            
            # Stop after reasonable time for profiling
            if yield_count >= 200:
                print(f"[{time.strftime('%X')}] Reached yield limit (200), stopping early")
                break
    except Exception as e:
        print(f"[{time.strftime('%X')}] ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    total_elapsed_ms = (time.perf_counter() - start_time) * 1000
    print(f"\n[{time.strftime('%X')}] === RESULTS ===")
    print(f"[{time.strftime('%X')}] Total yields: {yield_count}")
    print(f"[{time.strftime('%X')}] Total elapsed: {total_elapsed_ms:.1f}ms")
    print(f"[{time.strftime('%X')}] Avg per yield: {total_elapsed_ms/max(yield_count, 1):.1f}ms")
    
    # Save timing data
    output_path = cascade_dir / "workspace" / "logs" / "streaming_profile.json"
    with open(output_path, 'w') as f:
        json.dump(timing_data, f, indent=2)
    print(f"[{time.strftime('%X')}] Timing data saved to: {output_path}")
    
    return True

if __name__ == "__main__":
    try:
        success = profile_streaming()
        if not success:
            sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n[{time.strftime('%X')}] Profile interrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n[{time.strftime('%X')}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
