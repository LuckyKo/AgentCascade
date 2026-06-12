#!/usr/bin/env python3
"""
Quick validation script for Compressor agent regularization.

This script verifies that:
1. The _create_system_agent method exists and has the correct signature
2. The active_stack_remove method exists
3. The force_fresh parameter is properly used in _create_and_run_agent
4. The compression agent can be loaded
"""

import sys
import os

# Add the parent directory to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_cascade.execution_engine import ExecutionEngine
from agent_cascade.agent_pool import AgentPool


def test_create_system_agent_exists():
    """Test that _create_system_agent method exists."""
    print("✓ Testing _create_system_agent method exists...")
    assert hasattr(ExecutionEngine, '_create_system_agent'), "_create_system_agent method not found"
    
    # Check signature
    import inspect
    sig = inspect.signature(ExecutionEngine._create_system_agent)
    params = list(sig.parameters.keys())
    assert 'agent_class' in params, "Missing agent_class parameter"
    assert 'instance_name' in params, "Missing instance_name parameter"
    assert 'task' in params, "Missing task parameter"
    assert 'caller' in params, "Missing caller parameter"
    
    print("  ✓ Method exists with correct signature")


def test_active_stack_remove_exists():
    """Test that active_stack_remove method exists."""
    print("✓ Testing active_stack_remove method exists...")
    assert hasattr(AgentPool, 'active_stack_remove'), "active_stack_remove method not found"
    
    import inspect
    sig = inspect.signature(AgentPool.active_stack_remove)
    params = list(sig.parameters.keys())
    assert 'name' in params, "Missing name parameter"
    
    print("  ✓ Method exists with correct signature")


def test_force_fresh_parameter():
    """Test that _create_and_run_agent accepts force_fresh parameter."""
    print("✓ Testing force_fresh parameter in _create_and_run_agent...")
    assert hasattr(ExecutionEngine, '_create_and_run_agent'), "_create_and_run_agent method not found"
    
    import inspect
    sig = inspect.signature(ExecutionEngine._create_and_run_agent)
    params = list(sig.parameters.keys())
    assert 'force_fresh' in params, "Missing force_fresh parameter"
    
    print("  ✓ Method accepts force_fresh parameter")


def test_compressor_soul_exists():
    """Test that Compressor soul file exists."""
    print("✓ Testing Compressor_soul.md exists...")
    soul_path = os.path.join(os.path.dirname(__file__), 'agents', 'Compressor_soul.md')
    assert os.path.exists(soul_path), f"Compressor_soul.md not found at {soul_path}"
    
    with open(soul_path, 'r') as f:
        content = f.read()
        assert 'name: Compressor' in content, "Soul file doesn't identify as Compressor"
        assert 'tagline:' in content, "Soul file missing tagline"
    
    print("  ✓ Compressor_soul.md exists and is valid")


def test_agent_invoker_imports():
    """Test that agent_invoker module can be imported."""
    print("✓ Testing agent_invoker imports...")
    try:
        from agent_cascade.compression.agent_invoker import invoke_compression_agent
        print("  ✓ Module imports successfully")
    except ImportError as e:
        print(f"  ✗ Import failed: {e}")
        raise


def main():
    """Run all validation tests."""
    print("=" * 60)
    print("Compressor Agent Regularization - Validation Tests")
    print("=" * 60)
    print()
    
    try:
        test_create_system_agent_exists()
        test_active_stack_remove_exists()
        test_force_fresh_parameter()
        test_compressor_soul_exists()
        test_agent_invoker_imports()
        
        print()
        print("=" * 60)
        print("✓ All validation tests passed!")
        print("=" * 60)
        return 0
        
    except AssertionError as e:
        print()
        print("=" * 60)
        print(f"✗ Validation failed: {e}")
        print("=" * 60)
        return 1
    except Exception as e:
        print()
        print("=" * 60)
        print(f"✗ Unexpected error: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())