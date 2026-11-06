"""
ConfigPersistMixin — pool settings persistence, live UI disabled_tools, and template/config refresh. Moved verbatim from agent_pool.py (Phase 2).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_cascade.agents import Assistant
from agent_cascade.log import logger
from agent_cascade.settings_registry import LLM_CFG_PERSIST_KEYS
from agent_cascade.utils.atomic_json import atomic_write_json

from ..agent_instance import PoolSettings

# Sentinel key marking that the one-time class-defaults migration has been applied.
_CLASS_DEFAULTS_MIGRATED = '_class_defaults_migrated'


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

                # Add work folders + default workspace from operation_manager.
                # An empty list is a VALID persisted value (an intentional clear must
                # round-trip), so always write both lists when operation_manager exists —
                # no truthiness gate that would drop a deliberate "no folders" state.
                if hasattr(self, 'operation_manager') and self.operation_manager:
                    om = self.operation_manager
                    data['work_access_folders_ro'] = [str(p) for p in om.extra_work_folders_ro]
                    data['work_access_folders_rw'] = [str(p) for p in om.extra_work_folders_rw]
                    data['default_workspace'] = str(om.base_dir)

                # Add auto_security (BUG_0029): persist the LIVE pool flag — it is always
                # a real bool after __init__, so no shadow check needed.
                data['auto_security'] = bool(self.auto_security)

                # Add approval timeout settings (BUG_0029 Phase 2 Step 5: read from state directly)
                from agent_cascade.runtime_state import state as _runtime_state
                data['enable_approval_timeout'] = _runtime_state.enable_timeout
                data['approval_timeout_seconds'] = _runtime_state.approval_timeout_seconds

                # Add AFK settings (server-backed flag shared by WebUI + Telegram bridge)
                data['afk_enabled'] = bool(_runtime_state.afk_enabled)
                data['afk_message'] = _runtime_state.afk_message
                data['afk_mode'] = _runtime_state.afk_mode

                # Add async shell console window toggle
                data['enable_async_shell_console_window'] = bool(self._enable_async_shell_console_window)

                # Add compression_fraction as percentage (BUG_0029 Phase 2: read from live state)
                data['compression_fraction'] = round(_runtime_state.compression_fraction * 100, 1)

                # Preserve the one-time class-defaults migration sentinel across saves. This method
                # rebuilds `data` from settings.to_dict() + live state, which would otherwise drop a
                # disk-only key; carry it forward so _migrate_class_defaults_to_ui_config stays idempotent.
                if self._pool_settings_path.exists():
                    try:
                        with open(self._pool_settings_path, 'r', encoding='utf-8-sig') as f:
                            _existing = json.load(f)
                            if _existing.get(_CLASS_DEFAULTS_MIGRATED):
                                data[_CLASS_DEFAULTS_MIGRATED] = True
                    except (OSError, json.JSONDecodeError) as e:
                        logger.debug(f"[PoolSettings] Failed to read existing settings for key preservation: {e}")

                # Persist llm_cfg tool char limits and grep_spillover to pool_settings.json.
                # The key list is the registry's LLM_CFG_PERSIST_KEYS (single source of truth).
                if hasattr(self, 'llm_cfg') and isinstance(self.llm_cfg, dict):
                    for key in LLM_CFG_PERSIST_KEYS:
                        if key in self.llm_cfg:
                            data[key] = self.llm_cfg[key]

                # Atomic write (tmp + fsync + .bak + os.replace) so a reader never observes a
                # partially written pool_settings.json.
                atomic_write_json(self._pool_settings_path, data)
                # Bump the shared settings revision (single counter for all persist paths).
                _runtime_state.bump_settings_revision()
        except Exception as e:
            logger.error(f"[PoolSettings] Failed to save settings: {e}")

    def persisted_pool_settings(self) -> Dict[str, Any]:
        """Return the on-disk ``pool_settings.json`` as a dict ({} if missing/unreadable).

        Public accessor so callers (e.g. the settings-migration handler, which needs to
        know which keys the server already has) do not reach into ``_pool_settings_path``
        and re-read the file themselves.
        """
        path = getattr(self, '_pool_settings_path', None)
        if not path or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, json.JSONDecodeError) as e:
            logger.debug(f"[PoolSettings] Failed to read {path}: {e}")
            return {}
        return data if isinstance(data, dict) else {}

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
            afk_enabled_raw = data.pop('afk_enabled', None)
            afk_message_raw = data.pop('afk_message', None)
            afk_mode_raw = data.pop('afk_mode', None)
            enable_async_shell_console_window_raw = data.pop('enable_async_shell_console_window', None)

            # Replace settings with loaded values (defaults fill gaps for new fields)
            self.settings = PoolSettings.from_dict(data)

            # Apply disabled_tools with backwards-compatible validation
            if disabled_tools_raw:
                self._apply_loaded_disabled_tools(disabled_tools_raw)

            # Apply work folders after operation_manager is initialized
            # (This is called during __init__ before om exists, so defer to post-init).
            # Presence-based gate (not truthiness) so an intentional clear (empty lists)
            # round-trips: an explicit [] is a valid value, not "absent". The old
            # `hasattr(self, '_pending_work_folders')` guard was self-referential — this
            # load path was the only place that ever created the attribute — so it was
            # always False on a fresh pool and the folders were silently dropped at boot.
            if work_folders_ro_raw is not None or work_folders_rw_raw is not None:
                self._pending_work_folders = {
                    'ro': work_folders_ro_raw or [],
                    'rw': work_folders_rw_raw or [],
                }

            # Store default_workspace for later application
            if default_workspace_raw:
                self._pending_default_workspace = default_workspace_raw

            # Store auto_security (BUG_0029): populate the LIVE pool flag AND keep the
            # persistence shadow so launchers can detect "persisted value present".
            if auto_security_raw is not None:
                self.auto_security = bool(auto_security_raw)
                self._loaded_auto_security = bool(auto_security_raw)

            # Store approval timeout settings for later application in _apply_pending_config
            if enable_approval_timeout_raw is not None:
                self._pending_enable_approval_timeout = bool(enable_approval_timeout_raw)
            if approval_timeout_seconds_raw is not None:
                try:
                    self._pending_approval_timeout_seconds = int(approval_timeout_seconds_raw)
                except (ValueError, TypeError):
                    logger.warning(f"[PoolSettings] Invalid approval_timeout_seconds value, ignoring.")

            # Store AFK settings for later application in _apply_pending_config.
            # NOTE: always assign (even when the key is absent) — unlike the approval-timeout
            # pair above, these attrs are not declared in __init__, so a stale value left on a
            # prior pool instance sharing this object's state would leak across restarts.
            self._pending_afk_enabled = bool(afk_enabled_raw) if afk_enabled_raw is not None else False
            self._pending_afk_message = str(afk_message_raw) if afk_message_raw is not None else ''
            from agent_cascade.settings import normalize_afk_mode
            self._pending_afk_mode = normalize_afk_mode(afk_mode_raw)

            # Apply async shell console window toggle from disk if present
            if enable_async_shell_console_window_raw is not None:
                self._enable_async_shell_console_window = bool(enable_async_shell_console_window_raw)

            # Apply compression_fraction from disk if present (BUG_0029 Phase 2: single writer)
            compression_fraction_raw = data.pop('compression_fraction', None)
            if compression_fraction_raw is not None:
                try:
                    from agent_cascade.runtime_state import state as _runtime_state
                    _runtime_state.set_compression_fraction(float(compression_fraction_raw))
                    logger.info(f"[PoolSettings] Loaded compression_fraction={compression_fraction_raw}%")
                except (ValueError, TypeError):
                    logger.warning(f"[PoolSettings] Invalid compression_fraction value, ignoring.")

            # Restore llm_cfg tool char limits and grep_spillover from disk
            if hasattr(self, 'llm_cfg') and isinstance(self.llm_cfg, dict):
                # tool_result_max_chars
                val = data.pop('tool_result_max_chars', None)
                if val is not None:
                    try:
                        self.llm_cfg['tool_result_max_chars'] = int(val)
                    except (ValueError, TypeError):
                        pass

                # wild_read_truncation_chars
                val = data.pop('wild_read_truncation_chars', None)
                if val is not None:
                    try:
                        self.llm_cfg['wild_read_truncation_chars'] = int(val)
                    except (ValueError, TypeError):
                        pass

                # Boot-time safety: the live-update clamping handlers (config_handlers.py) only run on
                # runtime changes, so a stale persisted pair can invert target > threshold. Clamp when
                # both keys are present; absent keys keep their code defaults.
                _tr = self.llm_cfg.get('tool_result_max_chars')
                _wc = self.llm_cfg.get('wild_read_truncation_chars')
                if _tr is not None and _wc is not None and _wc > _tr:
                    logger.warning(f"[PoolSettings] wild_read_truncation_chars ({_wc}) exceeds "
                                   f"tool_result_max_chars ({_tr}); clamping to {_tr} at boot.")
                    self.llm_cfg['wild_read_truncation_chars'] = _tr

                # grep_char_limit
                val = data.pop('grep_char_limit', None)
                if val is not None:
                    try:
                        self.llm_cfg['grep_char_limit'] = int(val)
                    except (ValueError, TypeError):
                        pass

                # grep_spillover
                val = data.pop('grep_spillover', None)
                if val is not None:
                    self.llm_cfg['grep_spillover'] = bool(val)

                # shell_char_limit
                val = data.pop('shell_char_limit', None)
                if val is not None:
                    try:
                        self.llm_cfg['shell_char_limit'] = int(val)
                    except (ValueError, TypeError):
                        pass

                # code_char_limit
                val = data.pop('code_char_limit', None)
                if val is not None:
                    try:
                        self.llm_cfg['code_char_limit'] = int(val)
                    except (ValueError, TypeError):
                        pass

                # list_dir_char_limit
                val = data.pop('list_dir_char_limit', None)
                if val is not None:
                    try:
                        self.llm_cfg['list_dir_char_limit'] = int(val)
                    except (ValueError, TypeError):
                        pass

                # max_images_for_llm
                val = data.pop('max_images_for_llm', None)
                if val is not None:
                    try:
                        self.llm_cfg['max_images_for_llm'] = int(val)
                    except (ValueError, TypeError):
                        pass

                # ── Memory-hint feature (plan §7) — restore 4 keys into llm_cfg ──
                val = data.pop('memory_hint_enabled', None)
                if val is not None:
                    self.llm_cfg['memory_hint_enabled'] = bool(val)

                val = data.pop('memory_hint_threshold', None)
                if val is not None:
                    try:
                        self.llm_cfg['memory_hint_threshold'] = min(max(0.0, float(val)), 1.0)
                    except (ValueError, TypeError):
                        pass

                val = data.pop('memory_hint_max_entries', None)
                if val is not None:
                    try:
                        self.llm_cfg['memory_hint_max_entries'] = min(max(1, int(val)), 4)
                    except (ValueError, TypeError):
                        pass

                val = data.pop('memory_hint_query_chars', None)
                if val is not None:
                    try:
                        self.llm_cfg['memory_hint_query_chars'] = min(max(100, int(val)), 4000)
                    except (ValueError, TypeError):
                        pass

                val = data.pop('memory_hint_cooldown_seconds', None)
                if val is not None:
                    try:
                        self.llm_cfg['memory_hint_cooldown_seconds'] = min(max(0.0, float(val)), 86400.0)
                    except (ValueError, TypeError):
                        pass

                val = data.pop('memory_hint_skill_suggestions', None)
                if val is not None:
                    self.llm_cfg['memory_hint_skill_suggestions'] = bool(val)

                val = data.pop('memory_hint_skill_threshold', None)
                if val is not None:
                    try:
                        self.llm_cfg['memory_hint_skill_threshold'] = min(max(0.0, float(val)), 1.0)
                    except (ValueError, TypeError):
                        pass

                # ── Skill invalidation (adaptive count-cap) — restore 4 keys into llm_cfg ──
                val = data.pop('skill_auto_invalidate_enabled', None)
                if val is not None:
                    self.llm_cfg['skill_auto_invalidate_enabled'] = bool(val)

                val = data.pop('skill_active_target_k', None)
                if val is not None:
                    try:
                        self.llm_cfg['skill_active_target_k'] = min(max(0.1, float(val)), 5.0)
                    except (ValueError, TypeError):
                        pass

                val = data.pop('skill_active_min_cap', None)
                if val is not None:
                    try:
                        self.llm_cfg['skill_active_min_cap'] = min(max(1, int(val)), 200)
                    except (ValueError, TypeError):
                        pass

                val = data.pop('skill_active_max_cap', None)
                if val is not None:
                    try:
                        _min_cap = int(self.llm_cfg.get('skill_active_min_cap', 20) or 1)
                        self.llm_cfg['skill_active_max_cap'] = min(max(_min_cap, int(val)), 1000)
                    except (ValueError, TypeError):
                        pass

                # ── Skill-scoring constants (research §5/§7/§18 + D-SAFE) — restore into llm_cfg ──
                # Clamps come from the single source-of-truth table (settings.SKILL_SCORE_SETTINGS),
                # byte-identical to the config handlers / preview endpoint. Missing keys simply fall
                # through (pool/core.py reads .get() with defaults), so old files are safe. Unparseable
                # values are SKIPPED (key left unset) exactly as before — hence the explicit cast here:
                # clamp_skill_setting() swallows parse errors and returns the default, which would be a
                # behavior change for this restore path.
                from agent_cascade.settings import SKILL_SCORE_SETTINGS, clamp_skill_setting
                for _key in SKILL_SCORE_SETTINGS:
                    val = data.pop(_key, None)
                    if val is not None:
                        try:
                            self.llm_cfg[_key] = clamp_skill_setting(_key, SKILL_SCORE_SETTINGS[_key]['type'](val))
                        except (ValueError, TypeError):
                            pass

                # ── Always-protected (unremovable) meta-skill set — restore into llm_cfg ──
                # STRING setting: normalize to the canonical sorted comma-separated string
                # (case-insensitive, empty tokens ignored) so llm_cfg stays JSON-serializable for
                # _save_pool_settings(). A MISSING key leaves llm_cfg unset so the manager falls
                # back to the DEFAULT four skills (backward-compat, no crash); a blank value is
                # normalized by format_skill_always_protected() back to that same default.
                from agent_cascade.settings import format_skill_always_protected
                val = data.pop('skill_always_protected', None)
                if val is not None:
                    self.llm_cfg['skill_always_protected'] = format_skill_always_protected(val)

        except Exception as e:
            logger.error(
                f"[PoolSettings] Failed to load settings from {self._pool_settings_path}: {e}. Using defaults.")

    def _apply_loaded_disabled_tools(self, disabled_tools_raw):
        """Apply loaded disabled_tools config.

        Tool names are stored as-is (normalized only) WITHOUT filtering against the
        tool registry. Filtering unknown tools must happen at USE-time, not load-time:
        lazily-registered tools (send_message, media_gen, compress_context — registered
        by agent_factory.py) are not in the registry yet at boot, so load-time filtering
        would incorrectly drop them. The use-time resolver
        (utils.disabled_tools.resolve_disabled_tools_for_agent) filters against the fully
        populated registry once agents have been created.
        """
        from agent_cascade.utils.disabled_tools import normalize_disabled_tools

        try:
            if isinstance(disabled_tools_raw, dict):
                validated_dict = {}
                for agent_key, agent_tools in disabled_tools_raw.items():
                    # Store as a list (not a set): matches the migration's sorted-list format and
                    # keeps the value JSON-serializable for _save_pool_settings(). Downstream
                    # resolvers normalize either format via normalize_disabled_tools().
                    validated_dict[agent_key] = list(normalize_disabled_tools(agent_tools))
                self.set_ui_disabled_tools(validated_dict)
            elif isinstance(disabled_tools_raw, list):
                self.set_ui_disabled_tools(normalize_disabled_tools(disabled_tools_raw))
        except Exception as e:
            logger.warning(f"[disabled_tools] Failed to apply loaded disabled tools config: {e}")

    def _migrate_class_defaults_to_ui_config(self):
        """One-time migration: seed pool_settings.json with the old per-class tool defaults.

        Background: per-agent-class hardcoded tool defaults (resolver "Layer 3") were removed so
        the UI (pool_settings.json → _ui_disabled_tools) becomes the single source of truth. To
        avoid a behavior regression for users who never opened the UI, this migration seeds the
        user config with those same defaults on first run after upgrade.

        - Runs only if sentinel ``_class_defaults_migrated`` is absent from the saved file.
        - Never overrides an agent key the user already configured (``setdefault``).
        - For SYSTEM agents (Security, Compressor) we EXCLUDE ``shell_cmd`` so they keep read-only
          shell access via restricted_shell=True instead of losing the tool entirely.

        Called from AgentPool.__init__ after _load_pool_settings() (see pool/core.py), so it runs
        for BOTH fresh installs (no settings file yet) and existing installs (file present).
        """
        # Already migrated? Return early without touching anything.
        if self._pool_settings_path.exists():
            try:
                with open(self._pool_settings_path, 'r', encoding='utf-8-sig') as f:
                    if json.load(f).get(_CLASS_DEFAULTS_MIGRATED):
                        return  # already migrated
            except Exception:
                pass  # unreadable — proceed to migrate

        from agent_cascade.constants import (
            DEFAULT_SECURITY_DISABLED_TOOLS, DEFAULT_COMPRESSOR_DISABLED_TOOLS,
            DEFAULT_GENERALIST_DISABLED_TOOLS, DEFAULT_ORCHESTRATOR_DISABLED_TOOLS,
            DEFAULT_REVIEWER_DISABLED_TOOLS, DEFAULT_WRITER_DISABLED_TOOLS,
        )
        # System agents get restricted_shell=True via _create_system_agent — keep shell_cmd available.
        SYSTEM_CLASSES = {'Security', 'Compressor'}
        seed_full = {
            'Security': set(DEFAULT_SECURITY_DISABLED_TOOLS),
            'Compressor': set(DEFAULT_COMPRESSOR_DISABLED_TOOLS),
            'Generalist': set(DEFAULT_GENERALIST_DISABLED_TOOLS),
            'Orchestrator': set(DEFAULT_ORCHESTRATOR_DISABLED_TOOLS),
            'Reviewer': set(DEFAULT_REVIEWER_DISABLED_TOOLS),
            'Writer': set(DEFAULT_WRITER_DISABLED_TOOLS),
        }
        seed = {k: sorted(v - {'shell_cmd'} if k in SYSTEM_CLASSES else v) for k, v in seed_full.items()}

        # Seed into the live UI config; user's explicit entry always wins.
        with self._ui_disabled_tools_lock:
            existing = dict(self._ui_disabled_tools) if self._ui_disabled_tools else {}
        for agent_key, tools in seed.items():
            existing.setdefault(agent_key, tools)  # user's explicit entry wins
        self.set_ui_disabled_tools(existing)

        # Persist the sentinel as a top-level key (read-modify-write; NOT via _save_pool_settings,
        # which rebuilds its dict and would drop it). All other keys are preserved.
        # Best-effort: in tests with mocked api_router the path may not be a real Path object.
        try:
            with self._settings_save_lock:
                data = {}
                if self._pool_settings_path.exists():
                    try:
                        with open(self._pool_settings_path, 'r', encoding='utf-8-sig') as f:
                            data = json.load(f)
                    except Exception:
                        data = {}
                data[_CLASS_DEFAULTS_MIGRATED] = True
                atomic_write_json(self._pool_settings_path, data)
        except (OSError, TypeError, AttributeError):
            logger.debug('[MIGRATION] Could not persist sentinel to disk (non-Path or I/O error); '
                         'in-memory seeding still applied.')

        logger.info(
            '[MIGRATION] Seeded class defaults into UI config for %d agents '
            '(shell_cmd kept available for system agents)', len(seed))

    def _apply_pending_config(self):
        """Apply configuration loaded from pool_settings.json that requires operation_manager.

        Called after __init__ completes and operation_manager is available.
        """
        # Apply pending work folders (attribute is initialized to None in AgentPool.__init__;
        # getattr hardens the mixin-standalone / test path where it may be absent).
        if getattr(self, '_pending_work_folders', None):
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

        # Restore AFK flag (server-backed, shared by WebUI + Telegram bridge)
        if om:
            try:
                if hasattr(self, '_pending_afk_enabled'):
                    om.set_afk(
                        self._pending_afk_enabled,
                        getattr(self, '_pending_afk_message', None),
                        getattr(self, '_pending_afk_mode', None))
                elif hasattr(self, '_pending_afk_message'):
                    om.set_afk(getattr(om, 'afk_enabled', False), self._pending_afk_message)
            except Exception as e:
                logger.warning(f"[PoolSettings] Failed to restore AFK settings: {e}")

    def get_template(self, name: str) -> Optional[Assistant]:
        """Get template by name with case-insensitive fallback.

        This method provides robustness against case mismatches between the agent_class
        specified during instance creation and how templates are registered in the pool.
        Fallback chain: exact → lowercase → titlecase.
        For example, if 'Security' is passed but template is registered as 'security',
        or vice versa (e.g. tool_dispatcher lowercases to 'security' but key is 'Security'),
        this will still find it.

        Also performs BUG_0235 mtime-based staleness detection: templates are a startup
        cache, so without this an on-disk soul edit is never served to agents spawned
        later in the same process. See _reload_template_if_stale() for the full design.

        Args:
            name: Template name to look up (e.g., 'Security', 'coder', etc.)

        Returns:
            The Assistant template if found, None otherwise.

        Example:
            >>> template = pool.get_template('Security')  # Works even if registered as 'security'
        """
        if not name or not isinstance(name, str):
            return None

        # Resolve the registered key through the case-insensitive fallback chain so the
        # soul bookkeeping lookups below hit the right entry regardless of caller casing
        # ('Security'/'security' → whichever key the template is actually registered under).
        template = self.templates.get(name)
        key = name
        if template is None:
            template = self.templates.get(name.lower())
            key = name.lower()
        if template is None:
            template = self.templates.get(name.title())
            key = name.title()
        if template is None:
            return None

        # BUG_0235: single os.stat staleness check. get_template is called at spawn and a
        # few times per turn (engine/run_loop.py, engine/llm_call.py) but never on the
        # tokenization hot path, so a ~1us stat syscall here is negligible.
        source_path = self._template_source.get(key)
        if source_path is None:
            # Synthetic/inline template — no on-disk soul to watch. Return as-is.
            return template

        try:
            current_mtime = os.path.getmtime(source_path)
        except OSError:
            # Soul deleted between loads: keep serving the cached template rather than
            # crashing a spawn (plan edge case). Debug-level — exceptional but not fatal.
            logger.debug('[BUG_0235] soul file for template %r is gone (%s); keeping cached',
                         key, source_path)
            return template

        recorded_mtime = self._template_mtime.get(key)
        if recorded_mtime is not None and current_mtime == recorded_mtime:
            # Hot path: file untouched since last load → byte-identical, return cached.
            return template

        # mtime differs: re-read just this soul. _reload_template_if_stale() confirms via
        # content hash whether the bytes actually changed before swapping and notifying,
        # so an mtime-only touch never rebuilds the shared prefix cache.
        return self._reload_template_if_stale(key, source_path, current_mtime)

    def _reload_template_if_stale(self, key: str, source_path: str, current_mtime: float) -> Optional[Assistant]:
        """Reload a single template after its soul file's mtime changed (BUG_0235).

        Mirrors refresh_agents()'s before/after + conditional-notify pattern, but scoped
        to one template. The swap is committed and notify_config_changed() fired ONLY when
        the content hash actually changed — a bare touch (new mtime, identical bytes) just
        refreshes the recorded mtime with no notify, preserving the prefix-cache
        byte-stability design intent. load_agent_template() runs OUTSIDE _template_lock;
        the mutation itself is delegated to _register_template() (core.py), the shared
        single mutation point.
        """
        from agent_cascade.agent_factory import load_agent_template

        llm_cfg = (getattr(self.api_router, 'default_llm_cfg', {}) if self.api_router else {})
        old_hash = self._compute_template_hash(key)  # read the still-cached template

        try:
            new_template = load_agent_template(self, key, llm_cfg)
        except Exception as e:
            # Re-read failed (e.g. soul now malformed): keep serving the cached template,
            # but update the recorded mtime so we don't retry-load on every subsequent
            # lookup — a spawn must never crash because of a bad edit.
            logger.error('[BUG_0235] reload of template %r failed: %s; keeping cached', key, e)
            with self._template_lock:
                self._template_mtime[key] = current_mtime
            return self.templates.get(key)

        # Commit the reload, deduplicating concurrent staleness detections: if another
        # thread already registered this exact mtime while we were re-reading, it owns the
        # notify for this content — a second notify would bump _config_version twice for
        # one on-disk change (one spurious downstream prompt rebuild).
        with self._template_lock:
            first_to_apply = self._template_mtime.get(key) != current_mtime
            self._register_template(key, new_template, Path(source_path))
        new_hash = self._compute_template_hash(key)  # reads the freshly-registered template

        if first_to_apply and old_hash != new_hash:
            logger.info('[BUG_0235] template %r content changed on disk; triggering config update', key)
            self.notify_config_changed()
        else:
            logger.debug('[BUG_0235] template %r mtime changed but content identical; skipping notify', key)
        return new_template

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
            logger.warning(f"[tool_assignment] set_ui_disabled_tools called with non-dict type "
                           f"{type(disabled_tools_dict).__name__}, ignoring")
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
        # Note: after Layer 3 removal the resolver no longer unions hardcoded per-class
        # defaults — the result is exactly the user's UI config for this agent (plus the
        # safe baseline only when the agent has no explicit config at all).
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

        The whole reload runs under _template_lock so a concurrent on-demand reload
        (get_template → _reload_template_if_stale) can't interleave with the clear +
        re-discover. _discover_agents re-enters the RLock through _register_template.
        Note: the lock is intentionally held across the soul file I/O of
        load_agent_template() — refresh is a rare manual op, and a concurrent
        on-demand load of a not-yet-loaded agent simply waits for it to finish.
        """
        with self._template_lock:
            # Capture current state before reload
            old_template_keys = set(self.templates.keys())
            old_template_state = self._get_template_state()

            # Perform the reload. Clear the soul bookkeeping alongside templates so it
            # doesn't go stale — _discover_agents below repopulates it via
            # _register_template for whatever souls still exist on disk.
            self.templates.clear()
            self._template_source.clear()
            self._template_mtime.clear()
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
                    logger.info('[REFRESH] Template content modified, triggering config update')
                self.notify_config_changed()
            else:
                logger.debug('[REFRESH] No changes detected in agent templates, skipping notification')

    def notify_config_changed(self):
        """Signal that global configuration has changed (workspace dir, templates, etc).

        Increments _config_version, which triggers ExecutionEngine to rebuild system prompts.
        """
        self._config_version += 1
        logger.debug(f"[CONFIG] Global configuration version incremented to {self._config_version}")
