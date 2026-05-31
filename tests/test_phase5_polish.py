"""Tests for Phase 5 Polish fixes — Issue #11, #13, #9.

Issue #11: _InstanceConversationMapping keys()/items()/values() divergence
Issue #13: _sync_instance_conversations only called once (and silent data loss)
Issue #9: instance_state not populated for main session
"""
import threading
import pytest


def _make_mock_inst(conversation):
    """Create a mock instance with the attributes needed by _InstanceConversationMapping."""
    return type('MockInst', (), {
        'conversation': conversation,
        '_compression_lock': threading.Lock(),
    })()


class TestInstanceConversationMappingKeysItemsValues:
    """Test that keys(), items(), values() include dict-only entries (Issue #11)."""

    def test_keys_includes_dict_only_entries(self):
        """Session rename creates a dict-only entry; keys() should yield it."""
        from agent_cascade.agent_pool import _InstanceConversationMapping
        
        pool = type('MockPool', (), {'instances': {}})()
        mapping = _InstanceConversationMapping(pool)
        # Simulate a session rename: old name popped, new name written to dict only
        mapping['old_name'] = []  # goes into dict storage (no instance)
        
        assert 'old_name' in mapping  # __contains__ checks dict
        assert 'old_name' in list(mapping.keys())  # keys() should also include it

    def test_items_includes_dict_only_entries(self):
        """items() should yield dict-only entries alongside instance-backed ones."""
        from agent_cascade.agent_pool import _InstanceConversationMapping
        
        pool = type('MockPool', (), {'instances': {}})()
        mapping = _InstanceConversationMapping(pool)
        conv = [{'role': 'user', 'content': 'hello'}]
        mapping['orphan'] = conv
        
        items = dict(mapping.items())
        assert 'orphan' in items
        assert items['orphan'] is conv

    def test_values_includes_dict_only_entries(self):
        """values() should yield values for dict-only entries."""
        from agent_cascade.agent_pool import _InstanceConversationMapping
        
        pool = type('MockPool', (), {'instances': {}})()
        mapping = _InstanceConversationMapping(pool)
        conv = [{'role': 'assistant', 'content': 'hi'}]
        mapping['orphan'] = conv
        
        vals = list(mapping.values())
        assert conv in vals

    def test_keys_items_values_consistent_with_contains(self):
        """If __contains__ returns True, keys() must include it (Issue #11)."""
        from agent_cascade.agent_pool import _InstanceConversationMapping
        
        pool = type('MockPool', (), {'instances': {}})()
        mapping = _InstanceConversationMapping(pool)
        mapping['dict_only'] = []
        
        assert 'dict_only' in mapping
        assert 'dict_only' in list(mapping.keys())
        found = False
        for k, v in mapping.items():
            if k == 'dict_only':
                found = True
        assert found

    def test_instance_backed_takes_priority(self):
        """When a key exists in both instances and dict, instance value wins."""
        from agent_cascade.agent_pool import _InstanceConversationMapping
        
        pool = type('MockPool', (), {'instances': {}})()
        mapping = _InstanceConversationMapping(pool)
        
        # Write to dict storage first
        dict_conv = [{'role': 'user', 'content': 'from_dict'}]
        mapping['test'] = dict_conv
        
        # Now add an instance with same name but different conversation
        inst_conv = [{'role': 'assistant', 'content': 'from_instance'}]
        pool.instances['test'] = _make_mock_inst(inst_conv)
        
        # __getitem__ should return instance value (source of truth)
        assert mapping['test'] == inst_conv
        
        # items() and values() should also yield instance values
        items_dict = dict(mapping.items())
        assert items_dict['test'] == inst_conv


class TestSyncInstanceConversations:
    """Test that instance_conversations always reflects current instances (Issue #13)."""

    def test_new_instances_visible_after_first_access(self):
        """Sub-agents spawned after first access should appear in dict storage."""
        from agent_cascade.agent_pool import _InstanceConversationMapping, AgentPool
        from agent_cascade.llm.schema import Message
        
        pool = type('MockPool', (), {
            'instances': {},
        })()
        
        mapping = _InstanceConversationMapping(pool)
        # At this point, no instances exist
        
        # Now add a new instance after first creation
        inst_conv = [Message(role='user', content='test')]
        pool.instances['new_sub'] = _make_mock_inst(inst_conv)
        
        # Sync should pick up the new instance
        mapping._sync_from_instances()
        assert 'new_sub' in mapping
        assert mapping['new_sub'] == inst_conv

    def test_sync_preserves_dict_only_entries(self):
        """_sync_from_instances should NOT destroy dict-only entries (rename pattern)."""
        from agent_cascade.agent_pool import _InstanceConversationMapping
        
        pool = type('MockPool', (), {'instances': {}})()
        mapping = _InstanceConversationMapping(pool)
        
        # Simulate session rename: new name written to dict only
        renamed_conv = [{'role': 'user', 'content': 'renamed'}]
        mapping['new_name'] = renamed_conv
        
        # Sync should preserve the dict-only entry
        mapping._sync_from_instances()
        
        assert 'new_name' in mapping
        assert mapping['new_name'] is renamed_conv

    def test_sync_preserves_dict_only_while_adding_new_instances(self):
        """Sync with both new instances AND existing dict-only entries."""
        from agent_cascade.agent_pool import _InstanceConversationMapping
        
        pool = type('MockPool', (), {'instances': {}})()
        mapping = _InstanceConversationMapping(pool)
        
        # Add dict-only entry (rename pattern)
        renamed_conv = [{'role': 'user', 'content': 'renamed'}]
        mapping['new_name'] = renamed_conv
        
        # Add instance-backed entries
        inst1_conv = [{'role': 'assistant', 'content': 'inst1'}]
        pool.instances['agent1'] = _make_mock_inst(inst1_conv)
        
        # Sync should include both
        mapping._sync_from_instances()
        
        assert 'new_name' in mapping
        assert 'agent1' in mapping
        assert mapping['agent1'] == inst1_conv
        assert mapping['new_name'] is renamed_conv


class TestSubAgentStateMainSession:
    """Test that instance_state is populated for main session (Issue #9)."""

    def test_create_main_agent_instance_populates_instance_state(self):
        """create_main_agent_instance should register root in instance_state."""
        from agent_cascade.api_integration import create_main_agent_instance
        
        # Minimal pool mock with required methods/attributes
        created_instances = {}
        
        class MockAgentPool:
            def __init__(self):
                self.instances = created_instances
                self.instance_state = {}
            
            def create_instance(self, instance_name, agent_class, parent_instance, max_turns, conversation):
                from agent_cascade.agent_instance import AgentInstance
                inst = AgentInstance(
                    instance_name=instance_name,
                    agent_class=agent_class,
                    conversation=list(conversation),
                    is_active=False,
                    max_turns=max_turns,
                    parent_instance=parent_instance,
                    created_at=0.0,
                    last_activity=0.0,
                    compression_summary=None,
                    latest_marker_index=-1,
                )
                self.instances[instance_name] = inst
                return inst
        
        pool = MockAgentPool()
        instance = create_main_agent_instance(
            pool=pool,
            instance_name='Maine',
            system_message_content="You are Maine",
        )
        
        # instance_state should be populated under the actual instance name 'Maine'
        assert 'Maine' in pool.instance_state
        assert pool.instance_state['Maine']['active'] is False
        assert 'Maine' in pool.instance_state['Maine']['agent_name']
        assert len(pool.instance_state['Maine']['messages']) >= 1
        
        # Should also be registered under actual instance name
        assert 'Maine' in pool.instance_state
        assert pool.instance_state['Maine']['active'] is False