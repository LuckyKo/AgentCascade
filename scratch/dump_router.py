import json
import os
from pathlib import Path
import sys

# Add the project root to sys.path
sys.path.append(os.getcwd())

from agent_pool import AgentPool

def dump_router():
    # Mock llm_cfg
    llm_cfg = {'model': 'mock-default'}
    pool = AgentPool(llm_cfg=llm_cfg)
    
    print("Router Endpoints:")
    for ep_id, ep in pool.api_router.endpoints.items():
        print(f"  ID: {ep_id}, Name: {ep.name}, Model: {ep.model}, Base: {ep.api_base}")
    
    print("\nAgent Priorities:")
    for agent_type, priorities in pool.api_router.agent_priorities.items():
        print(f"  {agent_type}: {priorities}")
    
    print("\nDefault LLM CFG:")
    print(f"  {pool.api_router.default_llm_cfg}")

if __name__ == "__main__":
    dump_router()
