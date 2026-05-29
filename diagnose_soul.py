"""Diagnostic script to trace the soul loading issue."""
import os, sys
from pathlib import Path

print(f"CWD: {os.getcwd()}")
sys.path.insert(0, '.')

# Check if soul file exists at expected path
soul_path = Path('agents') / 'orchestrator_soul.md'
print(f"Soul path: {soul_path}")
print(f"Exists: {soul_path.exists()}")

if not soul_path.exists():
    print("ERROR: Soul file not found! This means load_agent falls to fallback.")
    sys.exit(1)

# Try loading the config
import yaml
config = yaml.safe_load(soul_path.read_text())
print(f"Config name: {config.get('name')}")
print(f"Config tagline: {config.get('tagline')[:50]}...")

# Try building system prompt (mimicking soul_loader.build_system_prompt)
system_prompt = f"You are {config.get('name', 'Assistant')}.\n"
if config.get('tagline'):
    system_prompt += f"{config.get('tagline')}\n"

identity = config.get('identity', {})
if identity:
    system_prompt += "\n## Who You Are\n"
    if identity.get('background'):
        system_prompt += f"{identity.get('background').strip()}\n"

print(f"\nSystem prompt first 300 chars:")
print(system_prompt[:300])

# Now try importing the agent factory (will fail due to openai, but let's see)
try:
    from agent_cascade.soul_loader import build_system_prompt
    sp = build_system_prompt(config)
    print(f"\nbuild_system_prompt produced {len(sp)} chars")
    print(sp[:200])
except Exception as e:
    print(f"\nError building system prompt: {e}")

print("\n=== DIAGNOSIS ===")
if "You are a helpful assistant" not in system_prompt:
    print("GOOD: System prompt does NOT contain 'You are a helpful assistant'")
else:
    print("BAD: System prompt CONTAINS 'You are a helpful assistant'")