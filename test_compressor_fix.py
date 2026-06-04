"""
Test script to verify the Compressor agent loading fix.

This script tests that agents are stored and retrieved correctly after the case-fix.
"""

import sys
from pathlib import Path

# Add the project root to path
sys.path.insert(0, str(Path(__file__).parent))

def test_main_branch():
    """Test the main branch (N:\work\WD\AgentCascade)."""
    print("=" * 60)
    print("Testing Main Branch")
    print("=" * 60)
    
    # Import after path modification
    from agent_pool import AgentPool
    
    # Create a minimal LLM config
    llm_cfg = {
        'model': 'test-model',
        'api_key': 'test-key',
        'max_input_tokens': 4096,
    }
    
    # Create pool with agents directory
    agents_dir = Path(__file__).parent / 'agents'
    print(f"Agents directory: {agents_dir}")
    print(f"Agents directory exists: {agents_dir.exists()}")
    
    if agents_dir.exists():
        soul_files = list(agents_dir.glob('*_soul.md'))
        print(f"Found {len(soul_files)} soul files:")
        for f in soul_files:
            print(f"  - {f.name}")
    
    try:
        pool = AgentPool(llm_cfg=llm_cfg, agents_dir=str(agents_dir))
        
        # Check if Compressor is loaded
        print("\nChecking agent templates...")
        print(f"Available agents: {list(pool.agents.keys())}")
        
        # Test case sensitivity
        compressor_upper = pool.get_agent('Compressor')
        compressor_lower = pool.get_agent('compressor')
        
        print(f"\nget_agent('Compressor'): {compressor_upper is not None}")
        print(f"get_agent('compressor'): {compressor_lower is not None}")
        
        if compressor_upper or compressor_lower:
            print("\n✓ SUCCESS: Compressor agent loaded correctly!")
            return True
        else:
            print("\n✗ FAILURE: Compressor agent not found!")
            return False
            
    except Exception as e:
        print(f"\n✗ EXCEPTION: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_unified_branch():
    """Test the unified branch (N:\work\WD\AgentCascade_unified)."""
    print("\n" + "=" * 60)
    print("Testing Unified Branch")
    print("=" * 60)
    
    # Switch to unified branch
    unified_path = Path(__file__).parent.parent / 'AgentCascade_unified'
    if not unified_path.exists():
        print(f"Unified branch not found at: {unified_path}")
        return False
    
    sys.path.insert(0, str(unified_path))
    
    try:
        from agent_cascade.agent_pool import AgentPool
        
        # Create a minimal LLM config
        llm_cfg = {
            'model': 'test-model',
            'api_key': 'test-key',
            'max_input_tokens': 4096,
        }
        
        # Create pool with agents directory
        agents_dir = unified_path / 'agents'
        print(f"Agents directory: {agents_dir}")
        print(f"Agents directory exists: {agents_dir.exists()}")
        
        if agents_dir.exists():
            soul_files = list(agents_dir.glob('*_soul.md'))
            print(f"Found {len(soul_files)} soul files:")
            for f in soul_files:
                print(f"  - {f.name}")
        
        pool = AgentPool(llm_cfg=llm_cfg, agents_dir=str(agents_dir))
        
        # Check if Compressor is loaded
        print("\nChecking agent templates...")
        print(f"Available agents: {list(pool.templates.keys())}")
        
        # Test case sensitivity
        compressor_upper = pool.get_agent('Compressor')
        compressor_lower = pool.get_agent('compressor')
        
        print(f"\nget_agent('Compressor'): {compressor_upper is not None}")
        print(f"get_agent('compressor'): {compressor_lower is not None}")
        
        if compressor_upper:
            print("\n✓ SUCCESS: Compressor agent loaded correctly!")
            return True
        else:
            print("\n✗ FAILURE: Compressor agent not found!")
            return False
            
    except Exception as e:
        print(f"\n✗ EXCEPTION: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    print("Compressor Agent Loading Test")
    print("=" * 60)
    
    # Test main branch
    main_result = test_main_branch()
    
    # Test unified branch
    unified_result = test_unified_branch()
    
    print("\n" + "=" * 60)
    print("Summary:")
    print(f"  Main Branch:       {'PASS' if main_result else 'FAIL'}")
    print(f"  Unified Branch:    {'PASS' if unified_result else 'FAIL'}")
    print("=" * 60)