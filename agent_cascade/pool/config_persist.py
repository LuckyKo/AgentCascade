"""
ConfigPersistMixin — pool settings persistence, live UI disabled_tools, and template/config refresh. Moved verbatim from agent_pool.py (Phase 2).
"""

from __future__ import annotations
import json
import hashlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from agent_cascade.log import logger
from agent_cascade.agents import Assistant
class ConfigPersistMixin:
    def _save_pool_settings(self):
        """Persist PoolSettings plus extra config (disabled_tools, work folders) to disk.

        Thread-safe via lock, fault-tolerant. Saves everything in a single file
        (pool_settings.json) with PoolSettings fields at the top level alongside
        extra keys like 'disabled_tools'.
        """
        try:
            with self._settings_save_lock:
                self._pool_settings_path.parent.mkdir(parents=True, exist_ok=True)

                # Start with PoolSettings data
                data = self.settings.to_dict()

                # Add disabled_tools config (under lock to ensure consistency)
                with self._ui_disabled_tools_lock:
                    if self._ui_disabled_tools:
                        data['disabled_tools'] = dict(self._ui_disabled_tools)

                # Add work folders from operation_manager if available
                if hasattr(self, 'operation_manager') and self.operation_manager:
                    om = self.operation_manager
                    if om.extra_work_folders_ro or om.extra_work_folders_rw:
                        data['work_access_folders_ro'] = [str(p) for p in om.extra_work_folders_ro]
                        data['work_access_folders_rw'] = [str(p) for p in om.extra_work_folders_rw]

                # Add default workspace if available
                if hasattr(self, 'operation_manager') and self.operation_manager:
                    data['default_workspace'] = str(self.operation_manager.base_dir)

                # Add auto_security if explicitly set
                if self._loaded_auto_security is not None:
                    data['auto_security'] = self._loaded_auto_security

                # Add approval timeout settings from operation_manager if available
                if hasattr(self, 'operation_manager') and self.operation_manager:
                    om = self.operation_manager
                    if hasattr(om, 'enable_timeout'):
                        data['enable_approval_timeout'] = om.enable_timeout
                    if hasattr(om, 'approval_timeout_seconds'):
                        data['approval_timeout_seconds'] = om.approval_timeout_seconds

                # Add async shell console window toggle
                data['enable_async_shell_console_window'] = bool(self._enable_async_shell_console_window)

                # Add compression_fraction as percentage (runtime-modifiable module-level setting)
                from agent_cascade.settings import COMPRESSION_DEFAULT_FRACTION
                data['compression_fraction'] = round(COMPRESSION_DEFAULT_FRACTION * 100, 1)

                # Persist llm_cfg tool char limits and grep_spillover to pool_settings.json
                if hasattr(self, 'llm_cfg') and isinstance(self.llm_cfg, dict):
                    for key in ('tool_result_max_chars', 'grep_char_limit', 'grep_spillover',
                                'shell_char_limit', 'code_char_limit', 'list_dir_char_limit',
                                'max_images_for_llm'):
                        if key in self.llm_cfg:
                            data[key] = self.llm_cfg[key]

                with open(self._pool_settings_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[PoolSettings] Failed to save settings: {e}")

    def _load_pool_settings(self):
        """Load PoolSettings plus extra config from disk. Fault-tolerant.

        Handles backwards compatibility: silently ignores unknown fields and applies
        only valid settings. For disabled_tools, filters out references to tools
        that no longer exist in the registry.
        """
        if not self._pool_settings_path.exists():
            return
        try:
            with open(self._pool_settings_path, 'r', encoding='utf-8-sig') as f:
                content = f.read().strip()
                if not content:
                    return
                data = json.loads(content)

            if not isinstance(data, dict):
                logger.warning(f"[PoolSettings] Config file is not a dictionary. Skipping.")
                return

            # Extract extra keys before passing to PoolSettings.from_dict (which filters unknowns)
            disabled_tools_raw = data.pop('disabled_tools', None)
            work_folders_ro_raw = data.pop('work_access_folders_ro', None)
            work_folders_rw_raw = data.pop('work_access_folders_rw', None)
            default_workspace_raw = data.pop('default_workspace', None)
            auto_security_raw = data.pop('auto_security', None)
            enable_approval_timeout_raw = data.pop('enable_approval_timeout', None)
            approval_timeout_seconds_raw = data.pop('approval_timeout_seconds', None)
            enable_async_shell_console_window_raw = data.pop('enable_async_shell_console_window', None)

            # Replace settings with loaded values (defaults fill gaps for new fields)
            self.settings = PoolSettings.from_dict(data)

            # Apply disabled_tools with backwards-compatible validation
            if disabled_tools_raw:
                self._apply_loaded_disabled_tools(disabled_tools_raw)

            # Apply work folders after operation_manager is initialized
            # (This is called during __init__ before om exists, so defer to post-init)
            if (work_folders_ro_raw or work_folders_rw_raw) and hasattr(self, '_pending_work_folders'):
                self._pending_work_folders = {
                    'ro': work_folders_ro_raw,
                    'rw': work_folders_rw_raw,
                }

            # Store default_workspace for later application
            if default_workspace_raw:
                self._pending_default_workspace = default_workspace_raw

            # Store auto_security for later application in create_app()
            if auto_security_raw is not None:
                self._loaded_auto_security = bool(auto_security_raw)

            # Store approval timeout settings for later application in _apply_pending_config
            if enable_approval_timeout_raw is not None:
                self._pending_enable_approval_timeout = bool(enable_approval_timeout_raw)
            if approval_timeout_seconds_raw is not None:
                try:
                    self._pending_approval_timeout_seconds = int(approval_timeout_seconds_raw)
                except (ValueError, TypeError):
                    logger.warning(f"[PoolSettings] Invalid approval_timeout_seconds value, ignoring.")

            # Apply async shell console window toggle from disk if present
            if enable_async_shell_console_window_raw is not None:
                self._enable_async_shell_console_window = bool(enable_async_shell_console_window_raw)

            # Apply compression_fraction from disk if present (overrides module default)
            compression_fraction_raw = data.pop('compression_fraction', None)
            if compression_fraction_raw is not None:
                try:
                    import agent_cascade.settings as settings_mod
                    val = float(compression_fraction_raw) / 100.0  # Convert percentage to fraction
                    val = min(settings_mod.COMPRESSION_MAX_FRACTION, max(settings_mod.COMPRESSION_MIN_FRACTION, val))
                    settings_mod.COMPRESSION_DEFAULT_FRACTION = val
                    logger.info(f"[PoolSettings] Loaded compression_fraction={compression_fraction_raw}%")
                except (ValueError, TypeError):
                    logger.warning(f"[PoolSettings] Invalid compression_fraction value, ignoring.")

            # Restore llm_cfg tool char limits and grep_spillover from disk
            if hasattr(self, 'llm_cfg') and isinstance(self.llm_cfg, dict):
                # tool_result_max_chars
                val = data.pop('tool_result_max_chars', None)
                if val is not None:
                    try: self.llm_cfg['tool_result_max_chars'] = int(val)
                    except (ValueError, TypeError): pass

                # grep_char_limit
                val = data.pop('grep_char_limit', None)
                if val is not None:
                    try: self.llm_cfg['grep_char_limit'] = int(val)
                    except (ValueError, TypeError): pass

                # grep_spillover
                val = data.pop('grep_spillover', None)
                if val is not None:
                    self.llm_cfg['grep_spillover'] = bool(val)

                # shell_char_limit
                val = data.pop('shell_char_limit', None)
                if val is not None:
                    try: self.llm_cfg['shell_char_limit'] = int(val)
                    except (ValueError, TypeError): pass

                # code_char_limit
                val = data.pop('code_char_limit', None)
                if val is not None:
                    try: self.llm_cfg['code_char_limit'] = int(val)
                    except (ValueError, TypeError): pass

                # list_dir_char_limit
                val = data.pop('list_dir_char_limit', None)
                if val is not None:
                    try: self.llm_cfg['list_dir_char_limit'] = int(val)
                    except (ValueError, TypeError): pass

                # max_images_for_llm
                val = data.pop('max_images_for_llm', None)
                if val is not None:
                    try: self.llm_cfg['max_images_for_llm'] = int(val)
                    except (ValueError, TypeError): pass

        except Exception as e:
            logger.error(f"[PoolSettings] Failed to load settings from {self._pool_settings_path}: {e}. Using defaults.")

    def _apply_loaded_disabled_tools(self, disabled_tools_raw):
        """Apply loaded disabled_tools config with backwards-compatible validation.

        Silently ignores references to tools that no longer exist in the registry.
        """
        from agent_cascade.tools.base import TOOL_REGISTRY
        from agent_cascade.utils.disabled_tools import normalize_disabled_tools
        from agent_cascade.constants import RUNTIME_REGISTERED_TOOLS

        known_tools = set(TOOL_REGISTRY.keys()) | RUNTIME_REGISTERED_TOOLS

        try:
            if isinstance(disabled_tools_raw, dict):
                validated_dict = {}
                for agent_key, agent_tools in disabled_tools_raw.items():
                    normalized = normalize_disabled_tools(agent_tools)
                    valid_tools = [t for t in normalized if t in known_tools]
                    ignored = set(normalized) - set(valid_tools)
                    if ignored:
                        logger.debug(f"[disabled_tools] Ignoring unknown tools from saved config for '{agent_key}': {ignored}")
                    validated_dict[agent_key] = valid_tools
                self.set_ui_disabled_tools(validated_dict)
            elif isinstance(disabled_tools_raw, list):
                normalized = normalize_disabled_tools(disabled_tools_raw)
                valid_tools = [t for t in normalized if t in known_tools]
                ignored = set(normalized) - set(valid_tools)
                if ignored:
                    logger.debug(f"[disabled_tools] Ignoring unknown tools from saved config (global): {ignored}")
                self.set_ui_disabled_tools(valid_tools)
        except Exception as e:
            logger.warning(f"[disabled_tools] Failed to apply loaded disabled tools config: {e}")

    def _apply_pending_config(self):
        """Apply configuration loaded from pool_settings.json that requires operation_manager.

        Called after __init__ completes and operation_manager is available.
        """
        # Apply pending work folders
        if hasattr(self, '_pending_work_folders') and self._pending_work_folders:
            om = getattr(self, 'operation_manager', None)
            if om:
                try:
                    ro_paths = self._pending_work_folders.get('ro', []) or []
                    rw_paths = self._pending_work_folders.get('rw', []) or []
                    om.set_extra_work_folders(ro_paths, rw_paths)
                    logger.info(f"[PoolSettings] Applied saved work folders: RO={len(ro_paths)}, RW={len(rw_paths)}")
                except Exception as e:
                    logger.warning(f"[PoolSettings] Failed to apply saved work folders: {e}")

        # Apply pending default workspace
        if hasattr(self, '_pending_default_workspace') and self._pending_default_workspace:
            om = getattr(self, 'operation_manager', None)
            if om:
                try:
                    ws_path = Path(self._pending_default_workspace).resolve()
                    if ws_path != om.base_dir:
                        om.set_base_dir(self._pending_default_workspace)
                        logger.info(f"[PoolSettings] Applied saved default workspace: {ws_path}")
                except Exception as e:
                    logger.warning(f"[PoolSettings] Failed to apply saved default workspace: {e}")

        # Restore approval timeout settings from pool_settings.json
        om = getattr(self, 'operation_manager', None)
        if om:
            try:
                if hasattr(self, '_pending_enable_approval_timeout'):
                    om.set_enable_timeout(self._pending_enable_approval_timeout)
                if hasattr(self, '_pending_approval_timeout_seconds'):
                    om.set_approval_timeout(self._pending_approval_timeout_seconds)
            except Exception as e:
                logger.warning(f"[PoolSettings] Failed to restore approval timeout settings: {e}")
    def get_template(self, name: str) -> Optional[Assistant]:
        """Get template by name with case-insensitive fallback.
        
        This method provides robustness against case mismatches between the agent_class
        specified during instance creation and how templates are registered in the pool.
        Fallback chain: exact → lowercase → titlecase.
        For example, if 'Security' is passed but template is registered as 'security',
        or vice versa (e.g. tool_dispatcher lowercases to 'security' but key is 'Security'),
        this will still find it.
        
        Args:
            name: Template name to look up (e.g., 'Security', 'coder', etc.)
            
        Returns:
            The Assistant template if found, None otherwise.
            
        Example:
            >>> template = pool.get_template('Security')  # Works even if registered as 'security'
        """
        if not name or not isinstance(name, str):
            return None
            
        template = self.templates.get(name)
        if template is None:
            template = self.templates.get(name.lower())
        if template is None:
            template = self.templates.get(name.title())
        return template
    def set_ui_disabled_tools(self, disabled_tools_dict: dict | None = None) -> None:
        """Update the live UI disabled_tools config from the settings panel.

        Thread-safe write that replaces the entire cache atomically.
        Called by ws_handlers.handle_update_config() when user changes tool assignments.

        Args:
            disabled_tools_dict: Per-agent disabled tools dict from UI (or empty dict to clear).
        """
        if disabled_tools_dict is None:
            disabled_tools_dict = {}
        elif not isinstance(disabled_tools_dict, dict):
            from agent_cascade.log import logger
            logger.warning(
                f"[tool_assignment] set_ui_disabled_tools called with non-dict type "
                f"{type(disabled_tools_dict).__name__}, ignoring"
            )
            return
        with self._ui_disabled_tools_lock:
            self._ui_disabled_tools = dict(disabled_tools_dict)

    def get_ui_disabled_tools_for_agent(self, agent_name: str, agent_type: str = '') -> set:
        """Get current disabled tools for a specific agent class from live UI config.

        Thread-safe read that returns a snapshot of the disabled set for this agent.
        Used by execution_engine during each turn to apply real-time tool assignments.

        Reuses the centralized resolver from utils.disabled_tools for the lookup
        chain (name → slugified → agent_type → lowercase type) to avoid duplication.

        Args:
            agent_name: Display/template name of the agent class.
            agent_type: Agent type string for defense-in-depth lookups.

        Returns:
            Set of tool names that should be disabled for this agent.
        """
        from agent_cascade.utils.disabled_tools import resolve_disabled_tools_for_agent

        # Shallow copy inside lock to guard against concurrent mutation
        with self._ui_disabled_tools_lock:
            dt = dict(self._ui_disabled_tools)

        if not dt:
            return set()

        # Use the centralized resolver to extract per-agent tools from our live cache.
        # We pass it as instance_override (highest priority layer) with no template_cfg,
        # so it only reads from the live cache and applies the standard lookup chain.
        # Note: The resolver includes defense-in-depth defaults here as well. This is
        # intentional because set union is idempotent and provides extra safety — any
        # tool disabled by either source remains disabled.
        return resolve_disabled_tools_for_agent(
            instance_override={'disabled_tools': dt},
            template_cfg=None,
            agent_name=agent_name,
            agent_type=agent_type,
        )
    def list_agents(self) -> List[str]:
        """Return all available agent template names."""
        return list(self.templates.keys())

    def get_agent_info(self, name: str) -> dict | None:
        """Return info dict for an agent template (name, tagline/description, tools).

        Used by the default prompt builder and internal lookups.
        Returns None if the template is not found.
        """
        template = self.get_template(name)
        if template is None:
            return None

        # Get active functions using the same logic as agents use at runtime
        # This respects disabled_tools configuration from UI settings
        # Defensive guard: fallback to empty list if method doesn't exist
        active_functions = getattr(template, '_get_active_functions', lambda: [])()
        active_tool_names = [f['name'] for f in active_functions]

        return {
            'name': getattr(template, 'name', name),
            'tagline': getattr(template, 'description', ''),
            'tools': active_tool_names,  # Now filtered by disabled_tools config
        }
    def _compute_template_hash(self, template_name: str) -> Optional[str]:
        """Compute a hash of the template's system message for change detection.
        
        Args:
            template_name: Name of the agent template
            
        Returns:
            SHA256 hex digest of the template content, or None if template not found.
            
        Note: This method is NOT thread-safe. For thread-safety, callers should hold
              an appropriate lock when calling refresh_agents().
        """
        try:
            template = self.templates.get(template_name)
            if template is None:
                return None
            
            # system_message is a plain str (per agent.py line 69), not a Message object
            # So we access it directly, not via .content attribute
            system_msg = getattr(template, 'system_message', '')
            
            # Create a deterministic string representation for hashing
            content_str = f"{template_name}|{system_msg}"
            return hashlib.sha256(content_str.encode('utf-8')).hexdigest()
        except Exception as e:
            logger.debug(f"Error computing hash for template {template_name}: {e}")
            return None
    
    def _get_template_state(self) -> Dict[str, Any]:
        """Get current state of templates for comparison.
        
        Returns:
            Dictionary mapping template names to their hashes
        """
        return {name: self._compute_template_hash(name) for name in self.templates.keys()}
    
    def refresh_agents(self):
        """Reload all agent souls and templates from disk.
        
        Compares before/after state (both template keys and content hashes).
        Only calls notify_config_changed() if something actually changed on disk.
        This prevents unnecessary cache invalidation when user clicks "Refresh Agents"
        but no files were modified.
        """
        # Capture current state before reload
        old_template_keys = set(self.templates.keys())
        old_template_state = self._get_template_state()
        
        # Perform the reload
        self.templates.clear()
        self._discover_agents(str(self.agents_dir))
        
        # Capture new state after reload
        new_template_keys = set(self.templates.keys())
        new_template_state = self._get_template_state()
        
        # Compare keys (agents added/removed)
        keys_changed = old_template_keys != new_template_keys
        
        # Compare content hashes (agent system prompts edited)
        # Only compare templates that exist in BOTH old and new states (intersection)
        all_hashes_match = True
        common_keys = old_template_keys & new_template_keys  # templates present before AND after reload
        for key in common_keys:
            if old_template_state.get(key) != new_template_state.get(key):
                all_hashes_match = False
                logger.debug(f"[REFRESH] Template content changed: {key}")
                break
        
        # Only notify if something actually changed
        if keys_changed or not all_hashes_match:
            if keys_changed:
                added = new_template_keys - old_template_keys
                removed = old_template_keys - new_template_keys
                logger.info(f"[REFRESH] Templates changed - Added: {added}, Removed: {removed}")
            else:
                # Content modification triggers config update, log at info level for visibility
                logger.info("[REFRESH] Template content modified, triggering config update")
            self.notify_config_changed()
        else:
            logger.debug("[REFRESH] No changes detected in agent templates, skipping notification")

    def notify_config_changed(self):
        """Signal that global configuration has changed (workspace dir, templates, etc).
        
        Increments _config_version, which triggers ExecutionEngine to rebuild system prompts.
        """
        self._config_version += 1
        logger.debug(f"[CONFIG] Global configuration version incremented to {self._config_version}")
