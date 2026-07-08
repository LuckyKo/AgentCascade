/**
 * AgentCascade Console — Frontend Application
 * 
 * Connects to the API server via WebSocket for real-time streaming.
 * Renders messages with markdown, handles editing, deletion, and approvals.
 */

// ── Markdown setup ───────────────────────────────────────────────────────────
marked.setOptions({
  breaks: true,
  gfm: true,
  highlight: (code, lang) => {
    if (lang && hljs.getLanguage(lang)) {
      try { return hljs.highlight(code, { language: lang }).value; } catch { }
    }
    return hljs.highlightAuto(code).value;
  },
});

// ── DOMPurify security configuration ─────────────────────────────────────────
// Hardened config: explicitly whitelist allowed tags/attributes for defense-in-depth.
if (typeof DOMPurify !== 'undefined') {
  DOMPurify.setConfig({
    ALLOWED_TAGS: ['b','i','u','em','strong','a','code','pre','blockquote',
                   'p','br','hr','ul','ol','li','table','thead','tbody',
                   'tr','th','td','img','details','summary','span','h1','h2','h3',
                   'h4','h5','h6','div','section','article','mark','del'],
    ALLOWED_ATTR: ['href','src','title','class','open','style','alt'],
    ALLOW_DATA_ATTR: true,
  });
}

// ── Constants ────────────────────────────────────────────────────────────────
const USER = 'user';
const ASSISTANT = 'assistant';
const SYSTEM = 'system';
const FUNCTION = 'function';
const DEFAULT_SESSION_NAME = 'Maine'; // Default session/agent name

// Tab ID prefix — all agents (including session primary) use 'sub-' prefix for their tabs.
const TAB_PREFIX = 'sub-';  // e.g., 'sub-Maine'

// ── Throttle configuration (all streaming/render timing constants in one place) ──
const THROTTLE = Object.freeze({
  PUSH_IMMEDIATE_MS: 30,       // ActivityBar push throttle
  RENDER_SUBAGENT_MS: 150,     // sub-agent streaming render throttle
  RENDER_ROOT_BASE_MS: 250,    // root agent base render throttle
  RENDER_ROOT_MAX_MS: 500,     // root agent max render throttle cap (adaptive)
  ACTIVITY_BAR_RENDER_MS: 200, // ActivityBar full render throttle
  GEN_STATS_MS: 500,           // gen stats update throttle (~2Hz)
  CONTROLS_MS: 1000,           // controls update throttle (~1Hz)
  TELEMETRY_MS: 2000,          // telemetry panel update throttle (~2s)
  RENDER_DIAG_THRESHOLD_MS: 100, // render duration diagnostic warning threshold
});

// Pre-compiled regexes for thinking blocks (consistent with backend)
const _TAG_THINK = 'think';
const _TAG_THOUGHT = 'thought';
const _THINK_BLOCK_ANCHORED_RE = new RegExp('^\\s*<(' + _TAG_THINK + '|' + _TAG_THOUGHT + ')>([\\s\\S]*?)(</\\1>|$)', 'i');
const _THINK_BLOCK_BRACKET_ANCHORED_RE = new RegExp('^\\s*\\[(' + _TAG_THINK.toUpperCase() + '|' + _TAG_THOUGHT.toUpperCase() + ')\\]([\\s\\S]*?)(\\[/\\1\\]|$)', 'i');
const _GEMMA_THOUGHT_ANCHORED_RE = /^\s*<\|channel>thought([\s\S]*?)(<channel\|>|$)/i;

// ── State ────────────────────────────────────────────────────────────────────
const state = {
  // Root agent messages stored in subAgents under session name (e.g., 'Maine')
  // — same structure as all other agents, no special treatment.
  subAgents: {},
  activeStack: [],
  approvals: [],
  generating: false,
  agents: [],
  agentIndex: 0,
  viewingAgentIndex: 0,
  sessionName: localStorage.getItem('agent-cascade-session-name') || DEFAULT_SESSION_NAME,
  connected: false,
  editingIndex: null,  // Which message index is being edited
  activeSubTab: null,
  genStats: {
    startTime: 0,
    firstTokenTime: 0,
    tokenCount: 0,
    lastContentLength: 0,
    active: false,
    // Throttle timestamps for streaming performance
    lastGenStatsUpdate: 0,       // For updateGenStats throttling (~2Hz)
        lastSubAgentRender: 0,      // For renderSubAgents throttling (~150ms during streaming)
        lastSubAgentRenderDuration: 0, // Track render duration for adaptive throttling
    lastContextBarUpdate: 0,    // For updateContextBar throttling (~1Hz during streaming)
    lastUiUpdate: 0,            // For activity bar throttling (~1Hz)
    lastControlsUpdate: 0,      // For updateControls throttling (~1Hz)
  lastTelemetryUpdate: 0,     // updateTelemetryPanel throttling (~2s)
  },
  totalTokens: 0,
  totalWords: 0,
  maxTokens: 32768,
  autoSecurity: false,
  activeSecurityChecks: new Set(),
  securityResponses: {},
  summary: "", // Active compression summary
  lastMemoryEditTime: 0, // Timestamp of last manual memory edit to prevent race condition reverts
  _lastIsGenerating: undefined, // For change detection in updateControls()
  closedTabs: new Set(JSON.parse(localStorage.getItem('agent-cascade-closed-tabs') || '[]')),
};

let ws = null;
let reconnectTimer = null;
// Root agent rendering state is now managed per-panel via panel.dataset.lastRenderedCount (same as sub-agents)

// Modern browsers optimize painting for hidden tabs automatically — no explicit check needed.
// The early-return check in stream_update was also removed to avoid render delays when switching back.

// Per-panel scroll lock state for ALL panels including root (managed via subAgentScrollLocks)
const subAgentScrollLocks = {};

// ── DOM refs ─────────────────────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
// Root panel scroll container is now created dynamically (same as sub-agents)
const chatInput = $('#chatInput');
const sendBtn = $('#sendBtn');
const continueBtn = $('#continueBtn');
const stopBtn = $('#stopBtn');
const pauseBtn = $('#pauseBtn');
const resetBtn = $('#resetBtn');
const agentSelect = $('#agentSelect');
const sessionNameInput = $('#sessionName');
const statusText = $('#statusText');
const connectionDot = $('#connectionDot');
const approvalBar = $('#approvalBar');
const mainTabBar = $('#mainTabBar');
// Root tab is now created dynamically — no static reference needed
const mainTabPanels = document.querySelector('.main-tab-panels');

// ── Active Agent Concept ──────────────────────────────────────────────────────
// All agents are equal — "root" is just the session's primary agent.
// The active agent is whichever agent the user is currently viewing,
// or the session's primary agent if no tab is selected.

// Get the name of the currently active (viewed) agent — the one whose tab is selected.
// Falls back to session name (primary agent) when no tab is selected.
function getActiveAgentName() {
    if (!state.activeSubTab) return state.sessionName;
    const prefix = 'sub-';
    return state.activeSubTab.startsWith(prefix) ? state.activeSubTab.slice(prefix.length) : state.sessionName;
}

// Get the tab ID for a given agent name.
function getAgentTabId(name) { return 'sub-' + name; }

// Check if an instance name matches the session's primary agent (was "root").
// Still needed for legitimate distinctions like supervisor display, session management.
function isSessionPrimaryAgent(name) {
  return name === state.sessionName;
}

// Legacy alias — kept for backward compat during transition.
// Prefer isSessionPrimaryAgent() in new code.
function isRootAgentName(name) {
  return isSessionPrimaryAgent(name);
}

function getActiveInstanceName() {
  if (!state.activeSubTab) return state.sessionName;
  return state.activeSubTab.substring(4);
}

/** Remove stale sub-agent entries after merging server state.
 *  Also resets activeSubTab to null if it points to a dismissed agent, preventing blank panel rendering. */
function cleanupStaleSubAgents(data, state) {
  // Remove agents that no longer exist on the server (e.g., dismissed agents)
  for (const name of Object.keys(state.subAgents)) {
    if (!(name in data.agent_instances)) {
      delete state.subAgents[name];
    }
  }
  // Reset active tab if it points to a now-dismissed agent — prevents blank panel rendering
  const activeAgentName = state.activeSubTab?.startsWith('sub-') ? state.activeSubTab.slice(4) : null;
  if (activeAgentName && !(activeAgentName in data.agent_instances)) {
    state.activeSubTab = null;
  }
}

const ActivityBar = {
  el: null,          // DOM ref to #globalActivityBar
  fifoEl: null,      // DOM ref to .activity-fifo
  queuedEl: null,    // DOM ref to .activity-queued
  lastRenderTime: 0, // Throttle timer for render()
  _lastPushTime: 0,      // Throttle timer for pushImmediate()
  
  // Queue banner refs
  queueBanner: null,       // DOM ref to #queueBanner
  queueMessageList: null,  // DOM ref to #queueMessageList
  
  // Cached values for pushImmediate dedup (avoids JSON.stringify overhead)
  _dedupInstance: null,
  _dedupPreview: '',
  _dedupWaiting: false,
  _dedupTokens: 0,
  
  init() {
    this.el = document.getElementById('globalActivityBar');
    if (!this.el) console.warn('ActivityBar.init(): #globalActivityBar not found in DOM');
    if (this.el) {
      this.fifoEl = this.el.querySelector('.activity-fifo');
      this.queuedEl = this.el.querySelector('.activity-queued');
    }
    
    // Queue banner initialization
    this.queueBanner = document.getElementById('queueBanner');
    this.queueMessageList = document.getElementById('queueMessageList');
    if (this.queueMessageList) {
      this.queueMessageList.addEventListener('click', (e) => {
        const dismissBtn = e.target.closest('.queue-message-dismiss');
        if (dismissBtn) {
          const item = dismissBtn.closest('.queue-message-item');
          if (item) {
            const index = Array.from(this.queueMessageList.children).indexOf(item);
            send({ type: 'dismiss_queue', instance_name: getActiveInstanceName(), message_index: index });
          }
        }
      });
    }
    
    const clearAllBtn = document.getElementById('clearAllQueueBtn');
    if (clearAllBtn) {
      clearAllBtn.addEventListener('click', () => {
        // Send dismiss all via WebSocket (index -1 clears entire queue)
        send({ type: 'dismiss_queue', instance_name: getActiveInstanceName(), message_index: -1 });
      });
    }
  },
  
  push(instanceName, text) {
    if (instanceName !== this.getFilterInstance()) return;
    this.render(text);
  },
  
  pushImmediate(instanceName, preview, isWaiting, tokenCount) {
    if (instanceName !== this.getFilterInstance()) return;
    if (!this.el || !this.fifoEl) return;
  
    // Fast dedup: skip if content hasn't changed since last call
    if (this._dedupInstance === instanceName &&
    this._dedupPreview === preview &&
    this._dedupWaiting === isWaiting &&
    this._dedupTokens === tokenCount) return;
  
    if (performance.now() - this._lastPushTime < THROTTLE.PUSH_IMMEDIATE_MS) return;
    this._lastPushTime = performance.now();
  
    this._dedupInstance = instanceName;
    this._dedupPreview = preview;
    this._dedupWaiting = isWaiting;
    this._dedupTokens = tokenCount;
  
    const agentData = state.subAgents[instanceName];
    const isActive = agentData?.active ?? false;
  
    this.el.classList.toggle('active', isActive);

    if (isActive) {
      let status = '';

      if (!isSessionPrimaryAgent(instanceName) && isWaiting) {
        status = 'Waiting for API slot...';
      } else if (preview !== undefined && preview != null) {
        status = (preview === '' || !preview.trim()) ? 'Streaming...' : preview;
      }

      const tokCount = tokenCount > 0 ? tokenCount : (agentData?.total_tokens ?? state.totalTokens);
      if (tokCount > 0) {
        const wordCount = agentData?.total_words ?? state.totalWords;
        status += ` (${wordCount} words, ${tokCount} tokens)`;
      }

      this.fifoEl.textContent = status || 'Agent Idle';
    } else {
      this.fifoEl.textContent = 'Agent Idle';
    }

    if (this.queuedEl) {
      this.queuedEl.style.display = agentData?.has_queued_messages ? 'block' : 'none';
    }

    // Render queue banner with actual message previews
    this.renderQueueBanner(agentData?.queued_messages);
  },
  
  getFilterInstance() {
    return getActiveInstanceName();
  },
  
  setActiveTab(tabId) {
    this.render();
  },
  
render(streamingText) {
    const now = performance.now();
    if (now - this.lastRenderTime < THROTTLE.ACTIVITY_BAR_RENDER_MS) return;
    this.lastRenderTime = now;

    if (!this.el || !this.fifoEl) return;

    const agentData = state.subAgents[this.getFilterInstance()];
    const isActive = agentData?.active ?? false;

    this.el.classList.toggle('active', isActive);

    if (isActive) {
      let status = '';

      if (!isSessionPrimaryAgent(this.getFilterInstance()) && agentData.is_waiting) {
        status = 'Waiting for API slot...';
      } else if (streamingText !== undefined) {
        status = (streamingText === '' || !streamingText.trim()) ? 'Streaming...' : streamingText;
      } else {
        const msgs = agentData?.messages || [];
        status = getActivityPreview(msgs[msgs.length - 1]) || 'Agent Idle';
      }

      if (agentData.total_tokens !== undefined) {
        status += ` (${agentData.total_words ?? state.totalWords} words, ${agentData.total_tokens ?? state.totalTokens} tokens)`;
      }

      this.fifoEl.textContent = status;
    } else {
      this.fifoEl.textContent = 'Agent Idle';
    }
      
    if (this.queuedEl) {
    this.queuedEl.style.display = agentData?.has_queued_messages ? 'block' : 'none';
    }

    // Render queue banner with actual message previews
    this.renderQueueBanner(agentData?.queued_messages);
  },
  
  /** Render the queue banner showing queued messages with dismiss buttons */
  renderQueueBanner(queuedMessages) {
    if (!this.queueBanner || !this.queueMessageList) return;

    // Hide banner if no queued messages
    if (!queuedMessages || queuedMessages.length === 0) {
      this.queueBanner.style.display = 'none';
      return;
    }

    this.queueBanner.style.display = 'flex';
    this.queueMessageList.innerHTML = '';

    queuedMessages.forEach((msg) => {
      const item = document.createElement('div');
      item.className = 'queue-message-item';

      const textSpan = document.createElement('span');
      textSpan.className = 'queue-message-text';
      textSpan.textContent = msg;
      textSpan.title = msg; // Full message on hover

      const dismissBtn = document.createElement('button');
      dismissBtn.className = 'queue-message-dismiss';
      dismissBtn.textContent = '✕';
      dismissBtn.title = 'Dismiss this queued message';

      item.appendChild(textSpan);
      item.appendChild(dismissBtn);
      this.queueMessageList.appendChild(item);
    });
  }
};

// New CWrite-style DOM refs
const btnToggleSettings = $('#btn-toggle-settings');

// Sticky auto-scroll: per-panel scroll lock state in subAgentScrollLocks (used for ALL panels including root)
const sidePanel = $('#side-panel');
const statusWords = $('#status-words');
const statusTokens = $('#status-tokens');
const statusTokensSec = $('#status-tokens-sec');
const statusGenInfo = $('#status-gen-info');
const statusModel = $('#status-model');
const statusSave = $('#status-save');
const settingFontSize = $('#setting-font-size');
const valFontSize = $('#val-font-size');
const settingLinesEnabled = $('#setting-lines-enabled');
const settingMaxContext = $('#setting-max-context');
const settingMaxTokens = $('#setting-max-tokens');
const settingSoundIntervention = $('#setting-sound-intervention');
const settingSoundCompleted = $('#setting-sound-completed');
const settingReadFileLimit = $('#setting-read-file-limit');
const valReadFileLimit = $('#val-read-file-limit');

const settingUserColor = $('#setting-user-color');
const settingAssistantColor = $('#setting-assistant-color');
const settingRawEditColor = $('#setting-raw-edit-color');
const settingTruncateTools = $('#setting-truncate-tools');
// Auto Agent Tab Focus toggle - controls whether tabs auto-switch when agent stack changes
const settingAutoTabFocus = $('#setting-auto-tab-focus');

const workAccessFoldersRW = $('#workAccessFoldersRW');
const workAccessFoldersRO = $('#workAccessFoldersRO');
const defaultWorkspace = $('#defaultWorkspace');

const settingVisionEnabled = $('#setting-vision-enabled');
const settingImageDetail = $('#setting-image-detail');
const settingMaxImageSize = $('#setting-max-image-size');
const insertImageBtn = $('#insertImageBtn');
const imageInput = $('#imageInput');
const insertDocBtn = $('#insertDocBtn');
const docInput = $('#docInput');

const settingMcpServers = $('#setting-mcp-servers');

const afkToggle = $('#afkToggle');
const autoSecurityToggle = $('#autoSecurityToggle');
const settingAfkMessage = $('#setting-afk-message');

// Approval timeout settings
const approvalTimeoutEnabled = $('#settingApprovalTimeoutEnabled');
const approvalTimeoutSeconds = $('#settingApprovalTimeoutSeconds');


// Range outputs
const ranges = [
  { input: $('#setting-temperature'), output: $('#val-temperature') },
  { input: $('#setting-top-p'), output: $('#val-top-p') },
  { input: $('#setting-top-k'), output: $('#val-top-k') },
  { input: $('#setting-min-p'), output: $('#val-min-p') },
  { input: $('#setting-repeat-penalty'), output: $('#val-repeat-penalty') },
  { input: $('#setting-presence-penalty'), output: $('#val-presence-penalty') },
  { input: $('#setting-frequency-penalty'), output: $('#val-frequency-penalty') },
  { input: $('#setting-shell-char-limit'), output: $('#val-shell-char-limit') },
  { input: $('#setting-code-char-limit'), output: $('#val-code-char-limit') },
];

// ── Initialization ───────────────────────────────────────────────────────────

// Side panel toggle
if (btnToggleSettings && sidePanel) {
  btnToggleSettings.addEventListener('click', () => {
    sidePanel.classList.toggle('collapsed');
  });
}

// Resizer for Right Panel
const sidePanelResizer = $('#side-panel-resizer');
if (sidePanelResizer && sidePanel) {
  let isResizing = false;
  const appContainer = $('.app');

  sidePanelResizer.addEventListener('mousedown', (e) => {
    isResizing = true;
    if (appContainer) appContainer.classList.add('resizing');
    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', stopResizing);
    e.preventDefault(); // Prevent text selection
  });

  function handleMouseMove(e) {
    if (!isResizing) return;
    const width = window.innerWidth - e.clientX;
    // Enforce limits (also defined in CSS but JS helps smooth it)
    if (width > 150 && width < window.innerWidth * 0.8) {
      sidePanel.style.width = `${width}px`;
    }
  }

  function stopResizing() {
    isResizing = false;
    if (appContainer) appContainer.classList.remove('resizing');
    document.removeEventListener('mousemove', handleMouseMove);
    document.removeEventListener('mouseup', stopResizing);
    localStorage.setItem('side-panel-width', sidePanel.style.width);
  }

  // Restore width
  const savedWidth = localStorage.getItem('side-panel-width');
  if (savedWidth) {
    sidePanel.style.width = savedWidth;
  }
}

// Sidebar toggle (Left)
const btnToggleSidebar = $('#btn-toggle-sidebar');
const appSidebar = $('#app-sidebar');
if (btnToggleSidebar && appSidebar) {
  btnToggleSidebar.addEventListener('click', () => {
    appSidebar.classList.toggle('collapsed');
  });
}

// Resizer for Left Sidebar
const sidebarResizer = $('#sidebar-resizer');
if (sidebarResizer && appSidebar) {
  let isResizing = false;
  const appContainer = $('.app');

  sidebarResizer.addEventListener('mousedown', (e) => {
    isResizing = true;
    appSidebar.classList.remove('collapsed');
    if (appContainer) appContainer.classList.add('resizing');
    document.addEventListener('mousemove', handleSidebarMouseMove);
    document.addEventListener('mouseup', stopSidebarResizing);
    e.preventDefault(); // Prevent text selection
  });

  function handleSidebarMouseMove(e) {
    if (!isResizing) return;
    const width = e.clientX;
    // Enforce limits: min 180px, max 50% of viewport
    if (width > 180 && width < window.innerWidth * 0.5) {
      appSidebar.style.width = `${width}px`;
    }
  }

  function stopSidebarResizing() {
    isResizing = false;
    if (appContainer) appContainer.classList.remove('resizing');
    document.removeEventListener('mousemove', handleSidebarMouseMove);
    document.removeEventListener('mouseup', stopSidebarResizing);
    localStorage.setItem('sidebar-width', appSidebar.style.width);
  }

  // Restore saved width and ensure sidebar is visible
  const savedSidebarWidth = localStorage.getItem('sidebar-width');
  if (savedSidebarWidth) {
    let width = parseInt(savedSidebarWidth);
    const minW = 180;
    const maxW = window.innerWidth * 0.5;
    if (isNaN(width)) width = 260; // fallback default
    width = Math.max(minW, Math.min(width, maxW));
    appSidebar.style.width = `${width}px`;
    appSidebar.classList.remove('collapsed');
  }
}

// Collapsible sub-sections
document.querySelectorAll('.sidebar-label, .settings-section-title').forEach(el => {
  el.addEventListener('click', (e) => {
    const section = e.target.closest('.sidebar-section') ||
      e.target.closest('.sessions-section') ||
      e.target.closest('.settings-section');
    if (section) {
      section.classList.toggle('collapsed');
    }
  });
});

// Session Manager DOM refs
const refreshSessionsBtn = $('#refreshSessionsBtn');
const sessionSearch = $('#sessionSearch');
const sessionsList = $('#sessionsList');

// State for sessions
let sessions = [];

// Fetch sessions from API
async function fetchSessions() {
  try {
    if (sessionsList) sessionsList.innerHTML = '<div class="sessions-loading">Loading...</div>';
    const res = await fetch('/api/sessions');
    const data = await res.json();
    sessions = data.sessions || [];
    renderSessions();
  } catch (err) {
    console.error('Failed to fetch sessions:', err);
    if (sessionsList) sessionsList.innerHTML = '<div class="sessions-placeholder">Error loading sessions.</div>';
  }
}

// Initial fetch
fetchSessions();

// Render session list
function renderSessions() {
  if (!sessionsList) return;
  const query = sessionSearch ? sessionSearch.value.toLowerCase() : '';
  const filtered = sessions.filter(s =>
    s.name.toLowerCase().includes(query) ||
    s.agent.toLowerCase().includes(query)
  );

  if (filtered.length === 0) {
    sessionsList.innerHTML = `<div class="sessions-placeholder">${query ? 'No matching sessions.' : 'No sessions found.'}</div>`;
    return;
  }

  sessionsList.innerHTML = filtered.map(s => `
    <div class="session-item" data-path="${escapeHtml(s.path.replace(/\\/g, '/'))}">
      <div class="session-item-header">
        <span class="session-item-name">${escapeHtml(s.name)}</span>
        <span class="session-item-agent">${escapeHtml(s.agent)}</span>
      </div>
      <div class="session-item-meta">
        <span>${formatDate(s.mtime * 1000)}</span>
        <span>${formatSize(s.size)}</span>
      </div>
    </div>
  `).join('');

  // Add click listeners scoped to sessionsList (old DOM is replaced each render, so no leak)
  sessionsList.querySelectorAll('.session-item').forEach(item => {
    item.addEventListener('click', () => {
      const path = item.dataset.path;
      loadSession(path);
    });
  });
}

// Save settings when connection inputs change
if ($('#setting-endpoint')) $('#setting-endpoint').addEventListener('change', saveSettings);
if ($('#setting-api-key')) $('#setting-api-key').addEventListener('change', saveSettings);
if ($('#setting-model')) $('#setting-model').addEventListener('change', saveSettings);

function loadSession(path) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    if (confirm('Load this session? Current unsaved state will be lost.')) {
      state.closedTabs.clear();
      localStorage.removeItem('agent-cascade-closed-tabs');
      ws.send(JSON.stringify({
        type: 'load_session',
        path: path
      }));
    }
  }
}

function formatDate(timestamp) {
  const date = new Date(timestamp);
  return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// Search and Refresh
if (sessionSearch) {
  sessionSearch.addEventListener('input', renderSessions);
}
if (refreshSessionsBtn) {
  refreshSessionsBtn.addEventListener('click', fetchSessions);
}

// Appearance settings
if (settingFontSize && valFontSize) {
  settingFontSize.addEventListener('input', (e) => {
    const val = e.target.value;
    valFontSize.textContent = val;
    document.documentElement.style.setProperty('--font-size-base', `${val}px`);
  });
}

if (settingLinesEnabled) {
  settingLinesEnabled.addEventListener('change', (e) => {
    const show = e.target.checked;
    document.documentElement.style.setProperty('--show-line-numbers', show ? 'block' : 'none');
  });
}

if (settingMaxContext) {
  settingMaxContext.addEventListener('change', () => {
    updateAllContextBars();
  });
}

// Appearance colors
if (settingUserColor) {
  settingUserColor.addEventListener('input', (e) => {
    document.documentElement.style.setProperty('--user-bg', e.target.value);
  });
}
if (settingAssistantColor) {
  settingAssistantColor.addEventListener('input', (e) => {
    document.documentElement.style.setProperty('--assistant-bg', e.target.value);
  });
}
if (settingRawEditColor) {
  settingRawEditColor.addEventListener('input', (e) => {
    document.documentElement.style.setProperty('--raw-edit-bg', e.target.value);
  });
}

// Ranges
ranges.forEach(r => {
  if (r.input && r.output) {
    r.input.addEventListener('input', (e) => {
      let val = parseFloat(e.target.value);
      if (e.target.step && e.target.step.includes('.')) {
        const decimals = e.target.step.split('.')[1].length;
        r.output.textContent = val.toFixed(decimals);
      } else {
        r.output.textContent = val;
      }
    });
  }
});

// ── Settings Persistence ─────────────────────────────────────────────────────

function saveSettings() {
  const s = getGenerateCfg();
  if (settingLinesEnabled) s['setting-lines-enabled'] = settingLinesEnabled.checked;
  if (settingSoundIntervention) s['setting-sound-intervention'] = settingSoundIntervention.checked;
  if (settingSoundCompleted) s['setting-sound-completed'] = settingSoundCompleted.checked;
  if (settingUserColor) s['setting-user-color'] = settingUserColor.value;
  if (settingAssistantColor) s['setting-assistant-color'] = settingAssistantColor.value;
  if (settingRawEditColor) s['setting-raw-edit-color'] = settingRawEditColor.value;
  if (settingFontSize) s['setting-font-size'] = settingFontSize.value;
  if (settingMaxContext) s['setting-max-context'] = settingMaxContext.value;
  if (settingTruncateTools) s['truncate-tools'] = settingTruncateTools.checked;
  // Save Auto Agent Tab Focus toggle state
  if (settingAutoTabFocus) s['auto-tab-focus'] = settingAutoTabFocus.checked;

  if (settingImageDetail) s['setting-image-detail'] = settingImageDetail.value;
  if (settingMaxImageSize) s['setting-max-image-size'] = settingMaxImageSize.value;
  if ($('#setting-mcp-enabled')) s['setting-mcp-enabled'] = $('#setting-mcp-enabled').checked;
  if (settingMcpServers) s['setting-mcp-servers'] = settingMcpServers.value;

  if (workAccessFoldersRW) s['work-access-folders-rw'] = workAccessFoldersRW.value;
  if (workAccessFoldersRO) s['work-access-folders-ro'] = workAccessFoldersRO.value;

  ranges.forEach(r => {
    if (r.input) s[r.input.id] = r.input.value;
  });

  if ($('#setting-max-turns')) s['max-turns'] = $('#setting-max-turns').value;
  if ($('#setting-auto-continue')) s['auto-continue'] = $('#setting-auto-continue').checked;
  if ($('#setting-inner-loop-detect')) s['inner-loop-detect'] = $('#setting-inner-loop-detect').checked;
  if ($('#setting-tool-result-max-chars')) s['tool-result-max-chars'] = $('#setting-tool-result-max-chars').value;
  if ($('#setting-idle-timeout')) s['idle-timeout'] = $('#setting-idle-timeout').value;
  if (settingVisionEnabled) s['vision-enabled'] = settingVisionEnabled.checked;
  if (afkToggle) s['afk-enabled'] = afkToggle.checked;
  if (settingAfkMessage) s['afk-message'] = settingAfkMessage.value;
  if (autoSecurityToggle) s['auto-security'] = autoSecurityToggle.checked;

  // Approval timeout settings
  if (approvalTimeoutEnabled) s['approval-timeout-enabled'] = approvalTimeoutEnabled.checked;
  if (approvalTimeoutSeconds) s['approval-timeout-seconds'] = approvalTimeoutSeconds.value;

  localStorage.setItem('agent-cascade-settings', JSON.stringify(s));
  
  if (state.connected) {
    send({ type: 'update_config', generate_cfg: getGenerateCfg() });
  }
  
  // Re-render to apply setting changes immediately (like context bar max value)
  updateAllContextBars();
}

function loadSettings() {
  try {
    const raw = localStorage.getItem('agent-cascade-settings') || localStorage.getItem('qwen-settings');
    if (!raw) return;
    const s = JSON.parse(raw);

    ranges.forEach(r => {
      if (r.input && s[r.input.id] !== undefined) {
        r.input.value = s[r.input.id];
        r.input.dispatchEvent(new Event('input'));
      }
    });

    if (settingFontSize && s['setting-font-size'] !== undefined) {
      settingFontSize.value = s['setting-font-size'];
      settingFontSize.dispatchEvent(new Event('input'));
    }

    if (settingMaxTokens && s['max_tokens'] !== undefined) {
      settingMaxTokens.value = s['max_tokens'];
      settingMaxTokens.dispatchEvent(new Event('input'));
    }

    if (settingMaxContext && s['setting-max-context'] !== undefined) {
      settingMaxContext.value = s['setting-max-context'];
      settingMaxContext.dispatchEvent(new Event('input'));
    }

    if (settingLinesEnabled && s['setting-lines-enabled'] !== undefined) {
      settingLinesEnabled.checked = s['setting-lines-enabled'];
      settingLinesEnabled.dispatchEvent(new Event('change'));
    }

    if (settingSoundIntervention && s['setting-sound-intervention'] !== undefined) {
      settingSoundIntervention.checked = s['setting-sound-intervention'];
    }

    if (settingSoundCompleted && s['setting-sound-completed'] !== undefined) {
      settingSoundCompleted.checked = s['setting-sound-completed'];
    }

    if (settingTruncateTools && s['truncate-tools'] !== undefined) {
      settingTruncateTools.checked = s['truncate-tools'];
    }

    // Load Auto Agent Tab Focus toggle state
    if (settingAutoTabFocus && s['auto-tab-focus'] !== undefined) {
      settingAutoTabFocus.checked = s['auto-tab-focus'];
    }

    if (settingUserColor && s['setting-user-color'] !== undefined) {
      settingUserColor.value = s['setting-user-color'];
      settingUserColor.dispatchEvent(new Event('input'));
    }
    if (settingAssistantColor && s['setting-assistant-color'] !== undefined) {
      settingAssistantColor.value = s['setting-assistant-color'];
      settingAssistantColor.dispatchEvent(new Event('input'));
    }
    if (settingRawEditColor && s['setting-raw-edit-color'] !== undefined) {
      settingRawEditColor.value = s['setting-raw-edit-color'];
      settingRawEditColor.dispatchEvent(new Event('input'));
    }

    if (s['api_base'] !== undefined) $('#setting-endpoint').value = s['api_base'];
    if (s['api_key'] !== undefined) $('#setting-api-key').value = s['api_key'];
    if (s['model'] !== undefined) $('#setting-model').value = s['model'];

    if (s['vision-enabled'] !== undefined) $('#setting-vision-enabled').checked = s['vision-enabled'];
    if (s['max-turns'] !== undefined) $('#setting-max-turns').value = s['max-turns'];
    if (s['auto-continue'] !== undefined) $('#setting-auto-continue').checked = s['auto-continue'];
    if (s['inner-loop-detect'] !== undefined) $('#setting-inner-loop-detect').checked = s['inner-loop-detect'];
    if (s['tool-result-max-chars'] !== undefined) {
      $('#setting-tool-result-max-chars').value = s['tool-result-max-chars'];
      $('#setting-tool-result-max-chars').dispatchEvent(new Event('input'));
    }
    if (s['idle-timeout'] !== undefined) {
      $('#setting-idle-timeout').value = s['idle-timeout'];
    }
    if (s['grep_char_limit'] !== undefined) {
      $('#setting-grep-char-limit').value = s['grep_char_limit'];
    }
    if (s['grep_spillover'] !== undefined) {
      $('#setting-grep-spillover').checked = s['grep_spillover'];
    }

    if (settingImageDetail && s['setting-image-detail'] !== undefined) {
      settingImageDetail.value = s['setting-image-detail'];
    }
    if (settingMaxImageSize && s['setting-max-image-size'] !== undefined) {
      settingMaxImageSize.value = s['setting-max-image-size'];
    }

    if ($('#setting-mcp-enabled') && s['setting-mcp-enabled'] !== undefined) {
      $('#setting-mcp-enabled').checked = s['setting-mcp-enabled'];
    }

    if (settingMcpServers && s['setting-mcp-servers'] !== undefined) {
      settingMcpServers.value = s['setting-mcp-servers'];
    }

    // Approval timeout settings
    if (approvalTimeoutEnabled && s['approval-timeout-enabled'] !== undefined) {
      approvalTimeoutEnabled.checked = !!s['approval-timeout-enabled'];
    }
    if (approvalTimeoutSeconds && s['approval-timeout-seconds'] !== undefined) {
      approvalTimeoutSeconds.value = s['approval-timeout-seconds'];
    }

    if (workAccessFoldersRW) {
      if (s['work-access-folders-rw'] !== undefined) {
        workAccessFoldersRW.value = s['work-access-folders-rw'];
      }
    }
    if (workAccessFoldersRO && s['work-access-folders-ro'] !== undefined) {
      workAccessFoldersRO.value = s['work-access-folders-ro'];
    }

    if (defaultWorkspace && s['default-workspace'] !== undefined) {
      defaultWorkspace.textContent = s['default-workspace'];
      defaultWorkspace.title = s['default-workspace'];
    }

    const editDefaultWSBtn = $('#editDefaultWS');
    if (editDefaultWSBtn && !editDefaultWSBtn.dataset.bound) {
      editDefaultWSBtn.dataset.bound = "true";
      editDefaultWSBtn.addEventListener('click', () => {
        const current = defaultWorkspace.textContent;
        const newVal = prompt('Enter new Default Workspace path (requires server restart to take full effect):', current);
        if (newVal !== null && newVal.trim() !== '' && newVal.trim() !== current) {
          const trimmed = newVal.trim();
          defaultWorkspace.textContent = trimmed;
          defaultWorkspace.title = trimmed + ' (Pending restart)';
          defaultWorkspace.style.color = 'var(--accent)';
          
          // Save to local settings
          const settings = JSON.parse(localStorage.getItem('agent-cascade-settings') || '{}');
          settings['default-workspace'] = trimmed;
          localStorage.setItem('agent-cascade-settings', JSON.stringify(settings));
          
          alert('Default workspace path saved to settings. A full server restart is recommended to properly re-initialize the Code Interpreter (Docker) and other core services in the new location.');
        }
      });
    }

    if (afkToggle && s['afk-enabled'] !== undefined) {
      afkToggle.checked = s['afk-enabled'];
    }
    if (settingAfkMessage && s['afk-message'] !== undefined) {
      settingAfkMessage.value = s['afk-message'];
    }
    if (autoSecurityToggle && s['auto-security'] !== undefined) {
      autoSecurityToggle.checked = s['auto-security'];
      state.autoSecurity = s['auto-security'];
    }

    if (afkToggle && afkToggle.checked && !state.generating) {
      checkAfkAutoReply();
    }
  } catch (e) {
    console.error('Failed to load settings', e);
  }
}

// Auto-save settings on any change in the panel (debounced for sliders/typing)
let _saveSettingsTimer;
function debouncedSaveSettings() {
  clearTimeout(_saveSettingsTimer);
  _saveSettingsTimer = setTimeout(saveSettings, 300);
}

if (sidePanel) {
  sidePanel.addEventListener('change', saveSettings);
  sidePanel.addEventListener('input', debouncedSaveSettings);
}

if (workAccessFoldersRW) workAccessFoldersRW.addEventListener('input', debouncedSaveSettings);
if (workAccessFoldersRO) workAccessFoldersRO.addEventListener('input', debouncedSaveSettings);

if (afkToggle) {
  afkToggle.addEventListener('change', () => {
    saveSettings();
    if (afkToggle.checked && !state.generating) {
      checkAfkAutoReply();
    } else if (!afkToggle.checked) {
      if (afkPendingTimer) clearTimeout(afkPendingTimer);
    }
  });
}

if (settingAfkMessage) {
  settingAfkMessage.addEventListener('input', debouncedSaveSettings);
}

if (autoSecurityToggle) {
  autoSecurityToggle.addEventListener('change', () => {
    state.autoSecurity = autoSecurityToggle.checked;
    saveSettings();
    // Notify backend of toggle change and re-render approvals
    send({ type: 'set_auto_security', enabled: state.autoSecurity });
    if (!state.autoSecurity) {
      // Turning OFF: clear active checks and security responses to prevent stale data
      state.activeSecurityChecks.clear();
      state.securityResponses = {};
    }
    renderApprovals();
  });
}

loadSettings();

// ── WebSocket ────────────────────────────────────────────────────────────────

function connect() {
  if (ws && ws.readyState <= 1) return;

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${location.host}/ws/chat`;
  ws = new WebSocket(url);

  ws.onopen = () => {
    state.connected = true;
    connectionDot.classList.add('connected');
    connectionDot.title = 'Connected';
    statusText.textContent = '';
    if (statusSave) statusSave.textContent = 'Connected';
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    // Sync session name with server on connect
    send({ type: 'set_session_name', name: state.sessionName });
    // Sync local settings (including default_workspace)
    send({ type: 'update_config', generate_cfg: getGenerateCfg() });
  };

  ws.onclose = () => {
    state.connected = false;
    connectionDot.classList.remove('connected');
    connectionDot.title = 'Disconnected';
    if (statusSave) statusSave.textContent = 'Disconnected';
    statusText.textContent = 'Disconnected — reconnecting...';
    scheduleReconnect();
  };

  ws.onerror = () => {
    state.connected = false;
    connectionDot.classList.remove('connected');
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      handleServerMessage(data);
    } catch (err) {
      console.error('[WS] Failed to process server message:', err.message);
    }
  };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, 2000);
}

function send(obj) {
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify(obj));
  }
}

// ── Audio Context ────────────────────────────────────────────────────────────

let audioCtx = null;

function playSound(type) {
  try {
    if (!audioCtx) {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (!AudioContext) return;
      audioCtx = new AudioContext();
    }

    if (audioCtx.state === 'suspended') {
      audioCtx.resume();
    }

    const oscillator = audioCtx.createOscillator();
    const gainNode = audioCtx.createGain();

    oscillator.connect(gainNode);
    gainNode.connect(audioCtx.destination);

    if (type === 'intervention' && settingSoundIntervention && settingSoundIntervention.checked) {
      // Alert sound: two short high pitched beeps
      oscillator.type = 'square';
      oscillator.frequency.setValueAtTime(800, audioCtx.currentTime);
      oscillator.frequency.setValueAtTime(1200, audioCtx.currentTime + 0.1);
      gainNode.gain.setValueAtTime(0.05, audioCtx.currentTime);
      gainNode.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.2);
      oscillator.start(audioCtx.currentTime);
      oscillator.stop(audioCtx.currentTime + 0.2);
    } else if (type === 'completed' && settingSoundCompleted && settingSoundCompleted.checked) {
      // Success sound: low to high
      oscillator.type = 'sine';
      oscillator.frequency.setValueAtTime(440, audioCtx.currentTime);
      oscillator.frequency.exponentialRampToValueAtTime(880, audioCtx.currentTime + 0.15);
      gainNode.gain.setValueAtTime(0.05, audioCtx.currentTime);
      gainNode.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.15);
      oscillator.start(audioCtx.currentTime);
      oscillator.stop(audioCtx.currentTime + 0.15);
    }
  } catch (e) {
    console.warn("Could not play sound:", e);
  }
}

// ── Server message handlers ──────────────────────────────────────────────────

function handleServerMessage(data) {
  const wasGenerating = state.generating;
  const prevApprovalsCount = (state.approvals || []).length;
  
  // Capture root agent state transition for 'done' event sound trigger
  let rootCompleted = false;
  if (data.type === 'done' && data.agent_instances) {
    const rootAgentData = data.agent_instances[state.sessionName];
    const prevRootState = state.subAgents[state.sessionName]?.agent_state;
    if (rootAgentData && prevRootState === 'RUNNING' && rootAgentData.agent_state === 'IDLE') {
      rootCompleted = true;
    }
  }

  switch (data.type) {
    case 'state':
    case 'done':
      // Full state update — ALL agents flow through agent_instances, root included

      // When paused mid-stream, preserve the last streamed message for EACH halted agent
      // in case it wasn't committed yet. Collect partials before merging server data.
      const partialContents = {};
      if (state.generating && data.instance_halted) {
        for (const [name, agentData] of Object.entries(state.subAgents)) {
          if (agentData?.is_halted && agentData?.messages?.length > 0) {
            partialContents[name] = String(agentData.messages[agentData.messages.length - 1]?.content || '');
          }
        }
      }

      // Merge ALL agent_instances including root — single source of truth, no legacy fallbacks
      if (data.agent_instances) {
        for (const [name, sa] of Object.entries(data.agent_instances)) {
          state.subAgents[name] = sa;
        }
        // Remove agents that no longer exist on the server (e.g., dismissed agents)
        cleanupStaleSubAgents(data, state);
      }
      // Restore partial content for each halted agent if server didn't already include it
      for (const [name, partialContent] of Object.entries(partialContents)) {
        const msgs = state.subAgents[name]?.messages || [];
        const lastServerContent = String(msgs[msgs.length - 1]?.content || '');
        if (!lastServerContent.startsWith(partialContent)) {
          msgs.push({ role: 'assistant', content: partialContent });
        }
      }
      // Normalize active_stack: backend sends tuples [name, depth], extract just name
      state.activeStack = (data.active_stack || []).map(e => Array.isArray(e) ? e[0] : e);
      state.generating = data.generating ?? false;
      if (data.agents) {
        const firstLoad = state.agents.length === 0;
        state.agents = data.agents;
        if (firstLoad) state.viewingAgentIndex = state.agentIndex;
        renderAgentSelect();
      }
      if (data.session_name) state.sessionName = data.session_name;
      if (data.agent_index !== undefined) state.agentIndex = data.agent_index;
      // FIX: Use Array.isArray check to update approvals (including empty array to clear all)
      if (Array.isArray(data.approvals)) {
        state.approvals = data.approvals;
      }
      renderApprovals(); // Always call to keep bar in sync, even when approvals field is missing

      if (data.total_tokens !== undefined) state.totalTokens = data.total_tokens;
      if (data.total_words !== undefined) state.totalWords = data.total_words;
      if (data.max_tokens !== undefined) state.maxTokens = data.max_tokens;
      if (data.summary !== undefined) state.summary = data.summary;
      if (data.instance_halted !== undefined) { state.instance_halted = data.instance_halted; state._serverHaltConfirmed = true; }

      // Telemetry: update panel with session telemetry from server
      if (data.telemetry) {
        updateTelemetryPanel(data.telemetry);
      }

      if (data.api_router) {
        if (!state.api_router) state.api_router = { endpoints: [], agent_priorities: {} };
        
        // Prevent overwriting local state and stealing focus if the user is actively editing
        const epList = document.getElementById('api-endpoints-list');
        const assignList = document.getElementById('agent-api-assignments');
        
        const isEditingEndpoints = epList && epList.contains(document.activeElement);
        const isEditingAssignments = assignList && assignList.contains(document.activeElement);
        
        if (!isEditingEndpoints) {
          state.api_router.endpoints = data.api_router.endpoints || [];
          renderApiEndpoints();
        }
        
        if (!isEditingAssignments) {
          // Client-side deduplication: normalize agent_priorities to prevent duplicate keys
          // from case mismatches between frontend (PascalCase) and backend (lowercase)
          const rawPriorities = data.api_router.agent_priorities || {};
          const normalizedPriorities = {};
          const seenLower = new Set();
          
          for (const [key, value] of Object.entries(rawPriorities)) {
            if (!key) continue;
            const keyLower = key.toLowerCase();
            if (!seenLower.has(keyLower)) {
              normalizedPriorities[key] = value;
              seenLower.add(keyLower);
            }
          }
          
          // Only update if we actually removed duplicates
          if (Object.keys(normalizedPriorities).length !== Object.keys(rawPriorities).length) {
            console.log('[WebSocket] Normalized agent_priorities:', 
              Object.keys(rawPriorities).length, '→', 
              Object.keys(normalizedPriorities).length, 'keys');
          }
          
          state.api_router.agent_priorities = normalizedPriorities;
          renderAgentApiAssignments();
        }
      }

      if (data.current_model && statusModel) {
        statusModel.textContent = data.current_model;
      }

      if (data.default_workspace && defaultWorkspace) {
        const s = JSON.parse(localStorage.getItem('agent-cascade-settings') || '{}');
        const localWS = s['default-workspace'];
        // Only let the server override if we don't have a local preference or it already matches
        if (!localWS || localWS === data.default_workspace) {
          defaultWorkspace.textContent = data.default_workspace;
          defaultWorkspace.title = data.default_workspace;
          defaultWorkspace.style.color = '';
        } else {
          // Keep the local one; the update_config sent on connect will sync the server eventually
          defaultWorkspace.textContent = localWS;
          defaultWorkspace.title = localWS + ' (Syncing with server...)';
          defaultWorkspace.style.color = 'var(--accent)';
        }
      }

      // Full state: force complete re-render (session load, reset, edit, delete, etc.)
      // Invalidate panel caches so edits/deletes trigger re-renders
      mainTabPanels.querySelectorAll('.messages').forEach(p => { 
        p.dataset.contentKey = ''; 
        p.dataset.lastRenderedCount = '999999999';
      });

      // Render all agents through the same path — no root/sub distinction
      renderSubAgents();
      // Reset throttle timer after full state render so subsequent stream_updates aren't throttled
      state.genStats.lastSubAgentRender = performance.now();
      state.genStats.lastSubAgentRenderDuration = 0;
      
      // Ensure the session primary agent's tab is active on initial load or if no tab is selected.
      if (!state.activeSubTab) {
        switchMainTab(getAgentTabId(state.sessionName));
      }

      // Ensure API endpoints and assignments are rendered after full state update.
      // Only re-render here if api_router wasn't in this message (already handled above).
      // This covers the case where api_router arrived in a prior message but agents arrived later.
      if (!data.api_router) {
        renderApiEndpoints();
        renderAgentApiAssignments();
      }
      updateControls();

      // Update stats if generating — pass active agent messages
      if (state.generating) {
        const activeMsgs = state.subAgents[getActiveAgentName()]?.messages || [];
        updateGenStats(activeMsgs);
      } else if (wasGenerating) {
        // Final update for stats — use active agent messages
        const activeMsgs = state.subAgents[getActiveAgentName()]?.messages || [];
        updateGenStats(activeMsgs, true);
        state.genStats.active = false;
        
        // Invalidate activity preview cache so next turn computes fresh preview
        delete state._lastActivityPreviewKey;
        delete state._lastActivityPreview;
        
        // Refresh telemetry config comparison after turn ends
        fetchTelemetry();
      }
      

      break;

    case 'stream_update': {
      // Only block stream updates when the ACTIVE agent itself is halted
      const activeName = getActiveAgentName();
      if (state.subAgents[activeName]?.is_halted) break;
      
      let completionDetected = false;
      
      const oldStackStr = (state.activeStack || []).join(',');
      // Track changes to decide render urgency:
      //   subAgentNewVisibleMessage — a new bubble was added to a VISIBLE panel (force immediate render)
      //   subAgentContentChanged   — any agent state changed (content streaming or new messages).
      //     All agents (including root) contribute to these flags via the unified agent_instances loop.

      let subAgentNewVisibleMessage = false;
      let subAgentContentChanged = false;
      
            // All agents (including root) flow through agent_instances — no special root path
      if (data.agent_instances) {
        for (const [name, sa] of Object.entries(data.agent_instances)) {
          const existing = state.subAgents[name];
          const prevMsgCount = existing ? existing.messages.length : 0;
          const wasActive = existing ? Boolean(existing.active) : false;
          const isNowActive = Boolean(sa.active);
          
                         // Completion detected when agent goes inactive AND is not on the active execution stack
                         // This prevents premature completion during race conditions or tool handoffs
                         if (wasActive && !isNowActive && state.activeStack.indexOf(name) === -1) {
            completionDetected = true;
          }
          
          if (sa.is_partial) {
            if (existing && existing.messages) {
              const hCount = sa.history_count || 0;
              
              // Skip stale updates that would truncate newer messages.
              // Use strict < so that updates with the same history_count are still processed —
              // during content streaming, history_count stays constant while message content grows.
              if (hCount < (existing._lastHistoryCount || 0)) {
                 // Stale: only sync metadata fields explicitly, don't touch message array.
                 const metaFields = ['active', 'is_halted', 'agent_class', 'has_queued_messages', 'queued_messages', 'is_waiting'];
                 for (const f of metaFields) {
                   if (sa[f] !== undefined) existing[f] = sa[f];
                 }
                existing._lastHistoryCount = hCount;
              } else {
                const startIdx = hCount - sa.messages.length;
                if (startIdx >= 0) {
                // Avoid holes: replace entirely if server is ahead, otherwise splice in
                  if (startIdx > existing.messages.length) {
                    existing.messages = [...sa.messages];
                  } else {
                    existing.messages.length = startIdx;
                    existing.messages.push(...sa.messages);
                  }
                } else {
                // Server rolled back (fewer messages than client). Replace entirely.
                  existing.messages = [...sa.messages];
                }
                // Sync other metadata fields — but NOT messages (we just merged those above).
                // Object.assign would overwrite our merged array with the partial sa.messages.
                const saCopy = { ...sa };
                delete saCopy.messages;
                Object.assign(existing, saCopy);
                // Defensive fallback: existing.messages is always set by the merge above,
                // but guard against malformed server data where Object.assign overwrites it.
                existing.messages = existing.messages || sa.messages || [];
                existing._lastHistoryCount = hCount;
                delete existing.is_partial; // local state should be complete
              }
            } else {
              // Fallback: if we don't have existing state, we can't merge partials
              state.subAgents[name] = sa;
            }
          } else {
          // Non-partial: replace entire agent state. Read from the updated state,
          // NOT from `existing` which still points to the OLD object after replacement.
            state.subAgents[name] = { ...sa, _lastHistoryCount: sa.history_count || 0 };
          }
          
          // Detect changes to decide render urgency:
          //   - New messages (message count grew or brand new agent) → force immediate render if visible
          //   - Any state change (including streaming content growth with same message count) → use flat ~200ms throttle
          const newMsgCount = state.subAgents[name]?.messages?.length ?? (sa.messages ? sa.messages.length : 0);
          const hasNewMessage = newMsgCount > prevMsgCount || !prevMsgCount;
          
          if (hasNewMessage) {
            subAgentContentChanged = true;
            // Only force-render new bubbles for panels that are actually visible —
            // avoid wasting DOM work on hidden panels.
            if (state.activeSubTab === 'sub-' + name) {
              subAgentNewVisibleMessage = true;
            }
          } else if (sa.is_partial && existing) {
            // Partial arrived with same message count — content is streaming in an existing bubble
            subAgentContentChanged = true;
          }
      }
      // Remove agents that no longer exist on the server (e.g., dismissed agents)
      cleanupStaleSubAgents(data, state);
      }  // end if (data.agent_instances)
      
      // Feed activity bar — happens on EVERY stream_update tick, before throttling
      const activeInstance = ActivityBar.getFilterInstance();
      const instanceData = state.subAgents[activeInstance];
      if (instanceData && instanceData.messages && instanceData.messages.length > 0) {
        const lastMsg = instanceData.messages[instanceData.messages.length - 1];
        ActivityBar.pushImmediate(activeInstance, getActivityPreview(lastMsg), instanceData.is_waiting ?? false, instanceData.total_tokens ?? 0);
      } else {
        ActivityBar.pushImmediate(activeInstance, '', instanceData?.is_waiting ?? false, instanceData?.total_tokens ?? 0);
      }

      // Normalize active_stack: backend sends tuples [name, depth], extract just name
      if (data.active_stack) state.activeStack = data.active_stack.map(e => Array.isArray(e) ? e[0] : e);
      // Reset throttle state if generation just started (was idle before this tick).
      // Server can initiate generation via stream_update without calling resetGenStats().
      if (!state.generating) {
        // Invalidate ALL panel caches (not just root) to force re-render on fresh generation
        mainTabPanels.querySelectorAll('.messages').forEach(p => {
          p.dataset.contentKey = '';
          p.dataset.lastRenderedCount = '999999999';
        });
        // Reset throttle timers so first render happens immediately, not delayed by 250ms+
        state.genStats.lastSubAgentRender = 0;
        state.genStats.lastSubAgentRenderDuration = 0;
      }
      state.generating = true;
      const newStackStr = (state.activeStack || []).join(',');
      const stackChanged = oldStackStr !== newStackStr;

      // Update scalar stats (always lightweight — no DOM work)
      if (data.total_tokens !== undefined) state.totalTokens = data.total_tokens;
      if (data.total_words !== undefined) state.totalWords = data.total_words;
      if (data.max_tokens !== undefined) state.maxTokens = data.max_tokens;
      if (data.current_model && statusModel) statusModel.textContent = data.current_model;
      if (data.telemetry) {
        state.pendingTelemetry = data.telemetry;
        const telemNow = performance.now();
        if (telemNow - state.genStats.lastTelemetryUpdate > THROTTLE.TELEMETRY_MS) {
          updateTelemetryPanel(data.telemetry);
          state.genStats.lastTelemetryUpdate = telemNow;
        }
      }

             const now = performance.now();
      
      // Approvals require immediate rendering (user must see these promptly)
      // FIX: Use Array.isArray check to update approvals (including empty array to clear all)
      if (Array.isArray(data.approvals)) {
        state.approvals = data.approvals;
      }
      renderApprovals(); // Always call to ensure bar stays in sync with current state

      // Throttle control updates to ~1Hz during streaming; always update when generating state changes.
      // Uses wasGenerating captured at function scope (line 770), before state.generating was updated above.
      if (wasGenerating !== state.generating || now - state.genStats.lastControlsUpdate > THROTTLE.CONTROLS_MS) {
        updateControls();
        state.genStats.lastControlsUpdate = now;
      }

      // Throttle sub-agent rendering during streaming to reduce markdown re-parsing load.
      if (!state.genStats.lastSubAgentRender) state.genStats.lastSubAgentRender = 0;
      const isSubAgentActive = state.activeStack && state.activeStack.length > 0;
      // Adaptive: if last render took long, increase throttle to avoid stacking renders.
      const lastRenderDur = Math.max(0, state.genStats.lastSubAgentRenderDuration || 0);
      const rootThrottleContent = Math.min(THROTTLE.RENDER_ROOT_MAX_MS, THROTTLE.RENDER_ROOT_BASE_MS + Math.round(lastRenderDur * 0.5));
      const subThrottleContent = isSubAgentActive ? THROTTLE.RENDER_SUBAGENT_MS : rootThrottleContent;
      
      // Force render on: completion detected, stack change, new visible message, 
      // or if the visible agent's content changed (bypass throttle for visible active agent).
      const isVisibleActiveAgentContentChanged = !!subAgentContentChanged && (state.activeSubTab === 'sub-' + activeName);
      
      const shouldRender = completionDetected || 
                           stackChanged || 
                           subAgentNewVisibleMessage || 
                           isVisibleActiveAgentContentChanged ||
                           (now - state.genStats.lastSubAgentRender > subThrottleContent);
      if (shouldRender) {
              // Measure render duration for adaptive throttling; reset timer AFTER to avoid stacking
              const renderStart = now;
      
        // Check if Auto Agent Tab Focus is enabled (default: true if setting not found or checked)
        const autoTabFocusEnabled = !settingAutoTabFocus || settingAutoTabFocus.checked;
        
        // Only call renderSubAgents if we're NOT about to call switchMainTab,
        // since switchMainTab calls renderSubAgents internally at the end.
        // This avoids redundant rendering when stackChanged triggers a tab switch.
        const willSwitchTab = autoTabFocusEnabled && stackChanged && (
          (state.activeStack.length > 0 && state.subAgents?.[state.activeStack[state.activeStack.length - 1]] && 
           state.activeSubTab !== 'sub-' + state.activeStack[state.activeStack.length - 1]) ||
          (state.activeStack.length === 0 && state.activeSubTab !== getAgentTabId(state.sessionName))
        );
        
        if (!willSwitchTab) {
          renderSubAgents();
          // Reset timer to ACTUAL post-render time so elapsed calculation includes render duration.
          // Using `now` (pre-render capture at line 1327) made the effective throttle window shorter than intended,
          // causing renders to stack during heavy markdown parsing and skip during tool pauses.
          const tEnd = performance.now();
          state.genStats.lastSubAgentRender = tEnd;
          state.genStats.lastSubAgentRenderDuration = tEnd - renderStart;
        } else {
          // switchMainTab will call renderSubAgents internally, but we still need to reset the throttle timer.
          // Set a sentinel so that after switchMainTab's internal renderSubAgents() completes,
          // the next stream_update tick won't immediately re-render (the tab just switched).
          state.genStats.lastSubAgentRender = performance.now();
          state.genStats.lastSubAgentRenderDuration = 0;
        }
        
        // Auto-switch tabs only when enabled and stack changed
        if (autoTabFocusEnabled && stackChanged) {
            if (state.activeStack.length > 0) {
              const topAgent = state.activeStack[state.activeStack.length - 1];
              // Only auto-switch if the sub-agent panel has actually been created
              if (state.subAgents && state.subAgents[topAgent] && state.activeSubTab !== 'sub-' + topAgent) {
                switchMainTab('sub-' + topAgent);
              }
            } else {
              // Auto-switch back to session primary agent when stack empties (if user isn't already on it)
              const primaryTab = getAgentTabId(state.sessionName);
              if (state.activeSubTab !== primaryTab) {
                switchMainTab(primaryTab);
              }
            }
          }
      }
        
      // Throttle gen stats to ~2Hz instead of ~6.5Hz. The token/sec display is
      // approximate anyway, so updating twice per second is visually indistinguishable
      // from the original frequency.
      if (!state.genStats.lastGenStatsUpdate) state.genStats.lastGenStatsUpdate = 0;
      if (now - state.genStats.lastGenStatsUpdate > THROTTLE.GEN_STATS_MS) {
        const activeMsgs = state.subAgents[getActiveAgentName()]?.messages || [];
        updateGenStats(activeMsgs);
        state.genStats.lastGenStatsUpdate = now;
      }
    }
    break;

    case 'approvals':
    // FIX: Use Array.isArray check to update approvals (including empty array to clear all)
    if (Array.isArray(data.approvals)) {
        state.approvals = data.approvals;
      }
      renderApprovals();
      break;

    case 'security_response': {
      const { request_id, response, verdict, reason } = data;
      state.activeSecurityChecks.delete(request_id);
      state.securityResponses[request_id] = { response, verdict, reason };

      // QoL: Auto-fill rejection field if security advisor said NO or timed out
      // (rendered via renderApprovals() triggered by state change broadcast)
      if (verdict === 'NO' || verdict === 'TIMEOUT') {
          const card = document.querySelector(`.approval-card[data-request-id="${request_id}"]`);
          if (card) {
              const rejectBtn = card.querySelector('.btn-danger');
              if (rejectBtn && !card.querySelector('.reject-input-area')) {
                  showRejectInput(request_id, rejectBtn);
              }
              const input = card.querySelector('.reject-reason-input');
              if (input) {
                  input.value = verdict === 'NO' && reason
                      ? reason
                      : 'Security advisor timed out after 180s. Please resubmit with clearer justification.';
              }
          }
      }
      break;
    }

    case 'error':
      state.generating = false;
      showInSystemToastBar(`⚠️ Error: ${data.message}`);
      delete state._lastActivityPreviewKey;
      delete state._lastActivityPreview;
      updateControls();
      break;
  }

  // Trigger sounds based on state changes
  const newApprovalsCount = (state.approvals || []).length;
  if (newApprovalsCount > prevApprovalsCount && !state.autoSecurity) {
    playSound('intervention');
  } else if (wasGenerating && !state.generating) {
    // Only play "completed" sound when the ROOT agent transitions RUNNING → IDLE
    // (sub-agent completions, pauses, and errors are excluded via rootCompleted check)
    if (data.type === 'done' && rootCompleted) {
      playSound('completed');
    }
    checkAfkAutoReply();
  }
}

// ── AFK Logic ────────────────────────────────────────────────────────────────

let lastAfkTime = 0;
let afkPendingTimer = null;

function checkAfkAutoReply() {
  if (afkToggle && afkToggle.checked) {
    const now = Date.now();
    const timeSinceLastAfk = now - lastAfkTime;
    const cooldown = 5 * 60 * 1000; // 5 minutes
    
    if (timeSinceLastAfk >= cooldown || lastAfkTime === 0) {
      // Send immediately (after a small delay to ensure UI updates)
      setTimeout(() => {
        if (!state.generating && afkToggle.checked) triggerAfkSend();
      }, 1000);
    } else {
      // Wait for the remaining time
      const remaining = cooldown - timeSinceLastAfk;
      if (afkPendingTimer) clearTimeout(afkPendingTimer);
      afkPendingTimer = setTimeout(() => {
        if (!state.generating && afkToggle.checked) {
          triggerAfkSend();
        }
      }, remaining);
    }
  }
}

function triggerAfkSend() {
  lastAfkTime = Date.now();
  const msg = (settingAfkMessage && settingAfkMessage.value.trim()) ? settingAfkMessage.value.trim() : 'User is AFK, continue working on given task or polish/verify your work if there are things to improve...';
  if (state.generating) return;
  
  chatInput.value = msg;
  autoResize(chatInput);
  sendMessage();
}

// ── Rendering ────────────────────────────────────────────────────────────────

// ── Unification Helper Functions ──────────────────────────────────────────────
// These helpers abstract the root vs sub-agent distinction for CSS classes and labels.
// All messages now use the same base CSS classes — differentiation is via data-agent-type attribute.

/** Return the combined CSS class string for a message element */
function msgClass(role) {
    return `message msg-${role}`;  // CSS class-based role differentiation (user, assistant, function, system, tool)
}

/** Return the CSS class for a message header element */
function headerClass() {
    return 'msg-header';
}

/** Return the CSS class for a message content element */
function contentClass() {
    return 'msg-content';
}

/** Return the CSS class for the role name/label element */
function nameLabelClass() {
    return 'msg-name';
}

/** Return the display label for a role — same logic for all agents including root */
function roleName(role, msg, instanceName) {
    // Unified labels: "You" for user everywhere, agent name for assistant, "Tool Result" everywhere
    if (role === 'user') return 'You';
    if (role === 'tool' || role === 'function') return 'Tool Result';
    
    // Assistant: show agent name from msg.name if available, then instanceName, then fallback
    if (msg.name) return msg.name;
    if (instanceName) return instanceName;  // Root also gets its instance name displayed now
    return 'Assistant';
}

/** Get config object for any agent rendering — all agents use the same path */
function getAgentConfig(name) {
    return { instanceName: name };
}

/**
 * Render a complete agent conversation as a DOM document fragment.
 * This is the unified rendering entry point — all agents (including root) go through this.
 * 
 * @param {string} instanceName - agent name (e.g., "Maine" for root, "coder" for sub-agent)
 * @param {Array}  messages     - array of message objects
 * @param {number} depth        - nesting level (0=root, 1=direct sub-agent, etc.)
 * @param {Array}  [indexMap]   - optional mapping from filtered-index → original-index
 *                                (needed when messages have been pre-filtered, e.g., system msgs removed)
 * @param {Object} [renderOpts] - optional render options (isGenerating flag for streaming state)
 * @returns {DocumentFragment}  fragment containing all rendered message elements
 */
function renderAgentConversation(instanceName, messages, depth, indexMap, renderOpts) {
    if (!messages || messages.length === 0) return document.createDocumentFragment();

    // All agents use the same config path — no special handling needed
    let config = getAgentConfig(instanceName);
    
    // Merge any render options (e.g., isGenerating from agentData.active)
    if (renderOpts) {
        config = Object.assign({}, config, renderOpts);
    }

    const fragment = document.createDocumentFragment();

    for (let i = 0; i < messages.length; i++) {
        const msg = messages[i];
        // Handle hole entries from sync gaps with a placeholder
        if (!msg) {
            const placeholderEl = document.createElement('div');
            placeholderEl.className = 'sub-msg sub-msg-unknown missed-msg';
            placeholderEl.dataset.index = i;
            const content = document.createElement('div');
            content.className = 'sub-msg-content';
            content.style = "font-style:italic;color:var(--text-dim);";
            content.textContent = '[... missed messages ...]';
            placeholderEl.appendChild(content);
            fragment.appendChild(placeholderEl);
            continue;
        }
        
        // Use original index from indexMap if provided, otherwise use the loop index
        const origIndex = indexMap ? indexMap[i] : i;
        const el = createMessageEl(msg, origIndex, config);

        fragment.appendChild(el);
    }

    return fragment;
}

function createMessageEl(msg, index, config) {
  // Default to session primary agent config if not provided (backward compatible)
  if (!config) config = getAgentConfig(state.sessionName);

  const div = document.createElement('div');
  div.className = msgClass(msg.role || 'unknown');
  div.dataset.index = index;
  
  // All agents get data-instance-name for per-agent accent color styling
  if (config.instanceName) {
      div.dataset.instanceName = config.instanceName;
  }

  const isEditable = !msg.function_call && msg.role !== 'function' && msg.role !== 'system';
  
  // Extract instanceName from config for edit/delete operations (all agents now have one)
  const instName = config.instanceName || null;

  // Header
  const header = document.createElement('div');
  header.className = headerClass();
  
  const nameSpan = document.createElement('span');
  nameSpan.className = nameLabelClass();
  nameSpan.textContent = roleName(msg.role || 'unknown', msg, instName);
  header.appendChild(nameSpan);

  // Actions
  const actions = document.createElement('div');
  actions.className = 'msg-actions';

  if (isEditable) {
    const editBtn = document.createElement('button');
    editBtn.className = 'msg-action-btn';
    editBtn.textContent = '✏️';
    editBtn.title = 'Edit message';
    editBtn.onclick = (e) => { e.stopPropagation(); startEdit(index, '', 0, instName); };
    actions.appendChild(editBtn);
  }

  const delBtn = document.createElement('button');
  delBtn.className = 'msg-action-btn msg-action-delete';
  delBtn.textContent = '🗑️';
  delBtn.title = 'Delete message';
  delBtn.onclick = (e) => { e.stopPropagation(); deleteMessage(index, instName); };
  actions.appendChild(delBtn);

  header.appendChild(actions);

  div.appendChild(header);

  // Double click edit
  div.addEventListener('dblclick', (e) => {
    const isAgentGenerating = instName 
      ? (state.subAgents[instName]?.active ?? state.generating)
      : state.generating;
    if (isAgentGenerating || !isEditable) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;

    let selectedText = sel.toString().trim();
    if (!selectedText) return;

    if (e.target.closest('.' + headerClass())) return;

    const contentDiv = div.querySelector('.' + contentClass());
    if (!contentDiv) return;

    const range = sel.getRangeAt(0);
    const preCaretRange = range.cloneRange();
    preCaretRange.selectNodeContents(contentDiv);
    preCaretRange.setEnd(range.startContainer, range.startOffset);
    const renderedOffset = preCaretRange.toString().length;
    const renderedLength = contentDiv.textContent.length;

    const proportion = renderedLength > 0 ? renderedOffset / renderedLength : 0;

    startEdit(index, selectedText, proportion, instName);
  });

  // Content
  const contentDiv = document.createElement('div');
  contentDiv.className = contentClass();

  let html = '';
  // Check config.isGenerating first (per-panel), then fall back to agent-specific active state
  const isGenerating = (config.isGenerating !== undefined)
    ? config.isGenerating
    : (state.subAgents[config.instanceName]?.active ?? false);

  // Handle reasoning/thinking content first (always shown if present)
  if (msg.reasoning_content) {
    html += renderThinkingBlock(msg.reasoning_content, isGenerating);
  }

  if (msg.function_call) {
    // Tool call bubble
    html += renderToolCall(msg);
  } else if (msg.role === 'function') {
    // Tool result bubble
    html += renderToolResult(msg);
  } else {
    // Regular text (user or assistant)
    let text = msg.content || '';
    
    // Deduplicate: If content starts with a thinking block that matches reasoning_content
    if (msg.reasoning_content) {
        const thinkMatch = text.match(_THINK_BLOCK_ANCHORED_RE) || text.match(_THINK_BLOCK_BRACKET_ANCHORED_RE);
        if (thinkMatch) {
            const embedded = thinkMatch[2].trim();
            const reasoning = msg.reasoning_content.trim();
            if (reasoning.includes(embedded) || embedded.includes(reasoning)) {
                // Remove the redundant block from content
                text = text.substring(thinkMatch[0].length).trim();
            }
        }
    }
    
    html += renderMarkdown(text, false); // Initial render is final
  }

  contentDiv.innerHTML = html;
  
  // Initialize streaming optimization dataset attributes
  // lastFlushTime ensures the 100ms flush window starts from bubble creation
  div.dataset.lastFlushTime = String(performance.now());
  div.dataset.prevContent = msg.content || '';
  div.dataset.prevReasoning = msg.reasoning_content || '';
  
  div.appendChild(contentDiv);
  return div;
}

// ── Bubble content updater ─────────────────────────────────────────────

/**
 * Updates the content of a message bubble with INCREMENTAL rendering support.
 * 
 * PERFORMANCE: During streaming, only renders the delta (new text appended)
 * instead of re-rendering the entire message. This avoids O(N) marked.parse()
 * on every tick when N is large (thousands of words).
 * 
 * Strategy:
 * - If curContent.startsWith(prevContent): content grew incrementally → render only delta and append
 * - Otherwise: full re-render (edit, delete, or structural change)
 */

/**
 * Attempt to append delta text into the last leaf element of a .msg-content div,
 * using insertAdjacentText (main branch approach - O(1) raw text append).
 * 
 * @param {HTMLElement} container - The .msg-content or .sub-msg-content div
 * @param {string} newText - The delta text to append
 * @returns {boolean} true if appended successfully, false if caller should fall back to full re-render
 */
function appendStreamingDelta(container, newText) {
    if (!newText || typeof newText !== 'string') return false;

    let target = container.lastElementChild;
    while (target && target.lastElementChild) {
        const child = target.lastElementChild;
        // Don't descend into inline formatting elements — they could trap raw text.
        // Also skip if target itself is inside a <pre> block, or child is/will be inside one.
        if (target.closest('pre') || ['code', 'strong', 'em', 'a'].includes(child.tagName.toLowerCase()) || child.closest('pre')) {
            break;
        }
        target = child;
    }
    if (!target) return false; // Edge case: empty container → caller falls through to full re-render
    try {
        target.insertAdjacentText('beforeend', newText);
        return true;
    } catch (e) {
        // Fallback: append as a text node directly to the container (safe, preserves existing content)
        container.appendChild(document.createTextNode(newText));
        return true;
    }
}

function updateBubbleContent(bubble, msg, config) {
    if (!config) config = getAgentConfig(state.sessionName);

    if (!msg) return;
    
    const contentDiv = bubble.querySelector('.' + contentClass());
    if (!contentDiv) return;

    // Performance: Check if content actually changed before re-rendering.
    // We still re-render the whole bubble to ensure correct Markdown formatting
    // (O(1) append breaks formatting), but we skip it if nothing changed.
    const prevContent = bubble.dataset.prevContent;
    const curContent = msg.content || '';
    const prevReasoning = bubble.dataset.prevReasoning;
    const curReasoning = msg.reasoning_content || '';
    const isGenerating = (config.isGenerating !== undefined) ? config.isGenerating : state.generating;

    // Reset incremental append counter when streaming is done or for fresh messages
    if (!isGenerating) {
        bubble.dataset.incrementCount = '0';
    }

    if (prevContent === curContent && prevReasoning === curReasoning && bubble.dataset.wasGenerating === String(isGenerating)) {
        return; // Nothing changed
    }
    
    bubble.dataset.prevContent = curContent;
    bubble.dataset.prevReasoning = curReasoning;
    bubble.dataset.wasGenerating = String(isGenerating);

    // FIX 2: Restore incremental path for plain-text messages only - prevents UI stuttering during long message streaming
    // This O(1) append avoids full renderMarkdown() re-parsing on every ~100ms tick for simple text streams.
    // Only applies to messages without function_call, reasoning_content, or function role (which need full re-render).
    if (isGenerating && prevContent !== undefined && !msg.function_call && msg.role !== 'function' && !msg.reasoning_content) {
        const newText = curContent.slice(prevContent.length);
        if (newText) {
            // FIX 4: Periodically force a full re-render every 8 incremental appends to prevent formatting drift.
            // Raw text append can diverge from markdown-structured state over many ticks.
            const incrementCount = parseInt(bubble.dataset.incrementCount || '0');
            try {
                appendStreamingDelta(contentDiv, newText);
                bubble.dataset.incrementCount = String(incrementCount + 1);
                if (incrementCount + 1 >= 8) {
                    bubble.dataset.incrementCount = '0'; // Reset counter after drift correction
                } else {
                    return;  // Success - skip full re-render
                }
            } catch(e) {
                // If incremental fails for any reason, fall through to full re-render below
                console.warn('Incremental streaming append failed, falling back to full render:', e);
            }
        }
    }

    let html = '';
    if (msg.reasoning_content) {
        html += renderThinkingBlock(msg.reasoning_content, isGenerating);
    }

    if (msg.function_call) {
        html += renderToolCall(msg);
    } else if (msg.role === 'function') {
        html += renderToolResult(msg);
    } else {
        let text = msg.content || '';
        if (msg.reasoning_content) {
            const thinkMatch = text.match(_THINK_BLOCK_ANCHORED_RE) || text.match(_THINK_BLOCK_BRACKET_ANCHORED_RE);
            if (thinkMatch) {
                const embedded = thinkMatch[2].trim();
                const reasoning = msg.reasoning_content.trim();
                if (reasoning.includes(embedded) || embedded.includes(reasoning)) {
                    text = text.substring(thinkMatch[0].length).trim();
                }
            }
        }
        // Use lightweight markdown during streaming to skip expensive thinking-block regex parsing.
        // Full parsing (allowThinking=true) is only needed on final renders when streaming stops.
        html += renderMarkdown(text, false);
    }
    
    // Preserve <details> open/close state and code block scroll positions during innerHTML replacement
    const details = contentDiv.querySelectorAll('details');
    const detailStates = Array.from(details).map(d => d.open);
    const codeScrollPositions = [];
    const codeBlocks = contentDiv.querySelectorAll('pre:not(.mermaid-container)');
    codeBlocks.forEach(cb => {
        codeScrollPositions.push({ element: cb, scrollTop: cb.scrollTop });
    });

    contentDiv.innerHTML = html;

    const newDetails = contentDiv.querySelectorAll('details');
    newDetails.forEach((d, i) => {
        if (i < detailStates.length) d.open = detailStates[i];
    });

    const newCodeBlocks = contentDiv.querySelectorAll('pre:not(.mermaid-container)');
    newCodeBlocks.forEach((cb, i) => {
        if (i < codeScrollPositions.length) cb.scrollTop = codeScrollPositions[i].scrollTop;
    });
}

function renderMarkdown(text, allowThinking = true) {
  if (!text || !text.trim()) return '';

  // PERFORMANCE OPTIMIZATION: Only perform expensive thinking-block parsing 
  // on final messages or if specifically requested. During streaming, we skip
  // O(N^2) regex work to keep the UI responsive.
  if (!allowThinking) {
    try {
      // Sanitize HTML output from marked to prevent XSS from LLM-generated content
      return DOMPurify.sanitize(marked.parse(text));
    } catch {
      return `<p>${escapeHtml(text)}</p>`;
    }
  }

  // Handle <think> tags or bracket [THINK] tags in content
  let thought = null;
  let isOpen = false;
  let before = '';
  let after = '';

  const thinkMatch = text.match(_THINK_BLOCK_ANCHORED_RE);
  const bracketMatch = !thinkMatch ? text.match(_THINK_BLOCK_BRACKET_ANCHORED_RE) : null;
  const gemmaMatch = (!thinkMatch && !bracketMatch) ? text.match(_GEMMA_THOUGHT_ANCHORED_RE) : null;

  if (thinkMatch) {
    thought = thinkMatch[2];
    const tag = thinkMatch[1];
    isOpen = !text.toLowerCase().includes('</' + tag.toLowerCase() + '>');
    // Since it's anchored to start, 'before' is just the leading whitespace if any
    before = text.substring(0, thinkMatch.index);
    after = text.substring(thinkMatch.index + thinkMatch[0].length);
  } else if (bracketMatch) {
    thought = bracketMatch[2];
    const tag = bracketMatch[1];
    isOpen = !text.toLowerCase().includes('[/' + tag.toLowerCase() + ']');
    before = text.substring(0, bracketMatch.index);
    after = text.substring(bracketMatch.index + bracketMatch[0].length);
  } else if (gemmaMatch) {
    thought = gemmaMatch[1];
    isOpen = !text.toLowerCase().includes('<channel|>');
    const startIdx = text.toLowerCase().indexOf('<|channel>thought');
    before = text.substring(0, startIdx);
    const endIdx = text.toLowerCase().indexOf('<channel|>');
    after = endIdx !== -1 ? text.substring(endIdx + 11) : '';
  }

  if (thought !== null) {
    let html = '';
    if (before.trim()) html += renderMarkdown(before, true);
    html += renderThinkingBlock(thought, isOpen);
    if (after.trim()) html += renderMarkdown(after, true);
    return html;
  }

  try {
    // Sanitize HTML output from marked to prevent XSS from LLM-generated content
    return DOMPurify.sanitize(marked.parse(text));
  } catch {
    return `<p>${escapeHtml(text)}</p>`;
  }
}

function renderToolCall(msg) {
  const fc = msg.function_call;

  // Special rendering for call_agent — show a human-readable delegation summary
  if (fc.name === 'call_agent') {
    let parsed;
    try {
      parsed = JSON.parse(fc.arguments);
    } catch {
      parsed = null;
    }
    const agentClass = parsed ? (parsed.agent_class || 'unknown') : 'unknown';
    const instanceName = parsed ? (parsed.instance_name || '') : '';
    const task = parsed ? (parsed.task || '') : '';

    let summaryLabel;
    if (task) {
      // Truncate long tasks for the summary line
      const shortTask = task.length > 120 ? task.substring(0, 120) + '…' : task;
      summaryLabel = `🤖 Delegated to <strong>${escapeHtml(agentClass)}</strong>: ${escapeHtml(shortTask)}`;
    } else {
      summaryLabel = `🤖 Delegated to <strong>${escapeHtml(agentClass)}</strong>`;
    }

    const argsHtml = parsed ? escapeHtml(JSON.stringify(parsed, null, 2)) : escapeHtml(fc.arguments || '');
    return `
      <details class="tool-call" open>
        <summary>${summaryLabel}</summary>
        <pre><code>${argsHtml}</code></pre>
      </details>
    `;
  }

  // Generic rendering for all other tool calls
  let argsHtml;
  try {
    const parsed = JSON.parse(fc.arguments);
    argsHtml = escapeHtml(JSON.stringify(parsed, null, 2));
  } catch {
    argsHtml = escapeHtml(fc.arguments || '');
  }
  return `
    <details class="tool-call" open>
      <summary>🛠️ <strong>${escapeHtml(fc.name)}</strong></summary>
      <pre><code>${argsHtml}</code></pre>
    </details>
  `;
}

function isToolFailure(msg) {
  if (msg.role !== 'function') return false;
  // Fast path: use the backend-provided tool_success flag.
  // This avoids any string scanning entirely for modern messages.
  if (typeof msg.tool_success === 'boolean') {
    return !msg.tool_success;
  }
  // Fallback for older log entries that lack the field:
  // Only check the first NON-EMPTY line of content — error markers always appear at the start.
  let firstLine = '';
  for (const line of (msg.content || '').split('\n')) {
    const stripped = line.trim();
    if (stripped) { firstLine = stripped.toLowerCase(); break; }
  }
  return firstLine.startsWith('error:') || 
         firstLine.startsWith('failed:') || 
         firstLine.startsWith('invalid:') ||
         firstLine.startsWith('permission denied:') ||
         firstLine.includes('rejected by user:') ||
         firstLine.includes('an error occurred') ||
         firstLine.includes('does not exist') ||
         firstLine.includes('failed to');
}

function renderToolResult(msg) {
  const content = msg.content || '';
  const isFail = isToolFailure(msg);
  const icon = isFail ? '❌' : '📋';
  const shouldTruncate = settingTruncateTools ? settingTruncateTools.checked : true;

  // Determine rendering strategy based on tool type and content characteristics.
  // Some tools return prose/markdown (web_extractor, ddg_search, calculate) that should be formatted.
  // Others return code-like output (code_interpreter, read_file, shell_cmd, grep, list_dir, write_file) that should stay in <pre><code>.
  const isCodeTool = ['code_interpreter', 'read_file', 'shell_cmd', 'grep', 'list_dir', 'write_file'].includes(msg.name);
  
  let contentHtml;
  if (msg.name === 'view_image' || content.match(/!\[.*?\]\(.*?\)/)) {
    // Image content: process through markdown to render images, rewriting file:/// URLs via backend proxy
    const truncatedForImage = (shouldTruncate && content.length > 2000) ? content.substring(0, 2000) + '\n\n... (truncated)' : content;
    const proxiedContent = truncatedForImage.replace(/!\[(.*?)\]\((?:file:\/\/\/|file:\/\/)(.*?)\)/g, '![image](/api/file?path=$2)');
    contentHtml = `<div class="tool-image-wrapper" style="padding-top: 8px;">${renderMarkdown(proxiedContent, false)}</div>`;
  } else if (!isCodeTool) {
    // Prose/markdown tools: process through renderMarkdown for proper formatting.
    // Do NOT truncate before rendering — truncating raw markdown mid-block produces malformed HTML.
    // The <details> element naturally keeps large content collapsed until expanded by the user.
    // If rendering fails (e.g., extremely large content), fall back to truncated <pre><code>.
    try {
      contentHtml = `<div class="tool-rendered-content">${renderMarkdown(content, false)}</div>`;
    } catch {
      const truncatedFallback = (shouldTruncate && content.length > 2000) ? content.substring(0, 2000) + '\n\n... (truncated)' : content;
      contentHtml = `<pre><code>${escapeHtml(truncatedFallback)}</code></pre>`;
    }
  } else {
    // Code-like tools: truncate before escaping to keep <pre><code> from bloating
    const truncated = (shouldTruncate && content.length > 2000) ? content.substring(0, 2000) + '\n\n... (truncated)' : content;
    contentHtml = `<pre><code>${escapeHtml(truncated)}</code></pre>`;
  }

  return `
    <details class="tool-result">
      <summary>${icon} Result from <strong>${escapeHtml(msg.name || 'tool')}</strong>${shouldTruncate && content.length > 2000 ? ` <span class="truncation-hint">(${content.length.toLocaleString()} chars)</span>` : ''}</summary>
      ${contentHtml}
    </details>
  `;
}

function renderThinkingBlock(thought, isOpen) {
  return `
    <details class="thinking-block" ${isOpen ? 'open' : ''}>
      <summary>💭 Thinking...</summary>
      <div class="thinking-content">${renderMarkdown(thought)}</div>
    </details>
  `;
}

    /**
     * Show a message in the system toast bar at the top of the chat area.
     * Used for errors, warnings, and notifications that don't belong in the main conversation flow.
     * 
     * @param {string} text - The message text to display (supports markdown via renderMarkdown)
     */
    function showInSystemToastBar(text) {
      const bar = document.getElementById('systemToastBar');
      if (!bar) return;

      // Enforce max capacity of 3 toasts — remove the oldest one first
      const existingToasts = bar.querySelectorAll('.system-toast-item');
      if (existingToasts.length >= 3 && existingToasts[0]) {
        existingToasts[0].remove();
      }

      const toast = document.createElement('div');
      toast.className = 'system-toast-item';
      toast.innerHTML = `
        <div class="msg-content">${renderMarkdown(text)}</div>
        <button class="toast-dismiss">×</button>
      `;
      const dismissBtn = toast.querySelector('.toast-dismiss');
      let autoDismissTimer = null;
      if (dismissBtn) {
        dismissBtn.onclick = () => {
          clearTimeout(autoDismissTimer); // Cancel pending auto-dismiss to avoid wasted timer
          toast.remove();
          if (!bar.querySelector('.system-toast-item')) bar.style.display = 'none';
        };
      }
      bar.appendChild(toast);
      bar.style.display = 'block';

      // Auto-dismiss after 15 seconds
      autoDismissTimer = setTimeout(() => {
        if (toast.parentNode) {
          toast.remove();
          if (!bar.querySelector('.system-toast-item')) bar.style.display = 'none';
        }
      }, 15000);
    }

// ── Message editing ──────────────────────────────────────────────────────────

let editClone = null;
function getEditClone(textarea) {
  if (!editClone) {
    editClone = document.createElement('div');
    editClone.className = 'edit-textarea message-edit-clone';
    document.body.appendChild(editClone);
  }
  const style = window.getComputedStyle(textarea);
  editClone.style.width = style.width;
  editClone.style.fontFamily = style.fontFamily;
  editClone.style.fontSize = style.fontSize;
  editClone.style.lineHeight = style.lineHeight;
  editClone.style.paddingLeft = style.paddingLeft;
  editClone.style.paddingRight = style.paddingRight;
  editClone.style.whiteSpace = 'pre-wrap';
  editClone.style.wordWrap = 'break-word';
  editClone.style.paddingTop = '0px';
  editClone.style.paddingBottom = '0px';
  editClone.style.minHeight = '0px';
  return editClone;
}

function startEdit(index, selectedText = '', proportion = 0, instanceName = null) {
  // All agents (including root) now have an instanceName and live in subAgents
  const msgs = state.subAgents[instanceName] ? state.subAgents[instanceName].messages : [];
  const msg = msgs[index];
  // Check if the specific agent is generating, not just the global root state
  const isAgentGenerating = instanceName 
    ? (state.subAgents[instanceName]?.active ?? state.generating)
    : state.generating;
  if (!msg || msg.function_call || msg.role === 'function' || isAgentGenerating) return;

  state.editingIndex = index;
  state.editingInstance = instanceName;

  const containerSelector = `#panelSub-${instanceName} .messages-scroll`;
  const scrollContainer = document.querySelector(containerSelector);
  if (!scrollContainer) return;

  const bubbleSelector = '.message';
  const bubbles = scrollContainer.querySelectorAll(bubbleSelector);
  const bubble = Array.from(bubbles).find(b => b.dataset.index === String(index));
  if (!bubble) return;


  const contentDiv = bubble.querySelector('.msg-content');
  const originalContent = msg.content || '';

  contentDiv.innerHTML = '';
  contentDiv.classList.add('editing');

  const textarea = document.createElement('textarea');
  textarea.className = 'edit-textarea';
  textarea.value = originalContent;
  textarea.dataset.index = index;

  const container = document.createElement('div');
  container.className = 'message-edit-container';

  const gutter = document.createElement('div');
  gutter.className = 'line-numbers-gutter';

  container.appendChild(gutter);
  container.appendChild(textarea);

  const toolbar = document.createElement('div');
  toolbar.className = 'edit-toolbar';

  const saveBtn = document.createElement('button');
  saveBtn.className = 'btn btn-primary btn-sm';
  saveBtn.textContent = 'Save';
  saveBtn.onclick = () => finishEdit(index, textarea.value, instanceName);

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn btn-secondary btn-sm';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.onclick = () => cancelEdit(index, instanceName);

  toolbar.appendChild(saveBtn);
  toolbar.appendChild(cancelBtn);

  contentDiv.appendChild(container);
  contentDiv.appendChild(toolbar);

  const updateGutter = () => {
    const clone = getEditClone(textarea);
    const lines = textarea.value.split('\n');
    let html = '';
    for (let i = 0; i < lines.length; i++) {
      clone.textContent = lines[i] || ' ';
      const height = clone.getBoundingClientRect().height;
      html += `<div style="height: ${height}px">${i + 1}</div>`;
    }
    gutter.innerHTML = html;
  };

  const autoResize = () => {
    textarea.style.height = 'auto';
    textarea.style.height = textarea.scrollHeight + 'px';
    updateGutter();
  };

  textarea.addEventListener('input', autoResize);

  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.ctrlKey) {
      e.preventDefault();
      finishEdit(index, textarea.value, instanceName);
    } else if (e.key === 'Escape') {
      cancelEdit(index, instanceName);
    }
  });

  // Calculate cursor
  let bestIdx = -1;
  if (selectedText) {
    const targetRawOffset = proportion * originalContent.length;
    let minDiff = Infinity;
    const safeText = selectedText.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

    let regex = new RegExp(`\\b${safeText}\\b`, 'gi');
    let wordMatchFound = false;
    let match;
    while ((match = regex.exec(originalContent)) !== null) {
      wordMatchFound = true;
      const diff = Math.abs(match.index - targetRawOffset);
      if (diff < minDiff) {
        minDiff = diff;
        bestIdx = match.index;
      }
    }

    if (!wordMatchFound) {
      regex = new RegExp(safeText, 'gi');
      while ((match = regex.exec(originalContent)) !== null) {
        const diff = Math.abs(match.index - targetRawOffset);
        if (diff < minDiff) {
          minDiff = diff;
          bestIdx = match.index;
        }
      }
    }
  }

  requestAnimationFrame(() => {
    autoResize();
    textarea.focus();
    if (bestIdx !== -1) {
      textarea.setSelectionRange(bestIdx, bestIdx + selectedText.length);
    }
  });
}

function finishEdit(index, newContent, instanceName = null) {
  if (state.editingIndex === index && state.editingInstance === instanceName) {
    state.editingIndex = null;
    state.editingInstance = null;
  }
  
  const msgs = state.subAgents[instanceName] ? state.subAgents[instanceName].messages : [];
  if (msgs[index]) msgs[index].content = newContent; // Optimistic update
  
  send({ type: 'edit_message', index, content: newContent, instance_name: instanceName });

  // Localized re-render — all agents use panelSub-{name} now
  const containerSelector = `#panelSub-${instanceName} .messages-scroll`;
  const scrollContainer = document.querySelector(containerSelector);
  if (!scrollContainer) return;

  const bubbleSelector = '.message';
  const bubbles = scrollContainer.querySelectorAll(bubbleSelector);
  const bubble = Array.from(bubbles).find(b => b.dataset.index === String(index));
  if (!bubble) return;

  // All agents use the same config now — no special handling needed
  const config = getAgentConfig(instanceName);
  bubble.querySelector('.' + contentClass()).classList.remove('editing');
  updateBubbleContent(bubble, msgs[index], config);
}

function cancelEdit(index, instanceName = null) {
  if (state.editingIndex === index && state.editingInstance === instanceName) {
    state.editingIndex = null;
    state.editingInstance = null;
  }

  // Localized re-render — all agents use panelSub-{name} now
  const containerSelector = `#panelSub-${instanceName} .messages-scroll`;
  const scrollContainer = document.querySelector(containerSelector);
  if (!scrollContainer) return;

  const bubbleSelector = '.message';
  const bubbles = scrollContainer.querySelectorAll(bubbleSelector);
  const bubble = Array.from(bubbles).find(b => b.dataset.index === String(index));
  if (!bubble) return;

  // All agents use the same config now — no special handling needed
  const config = getAgentConfig(instanceName);
  bubble.querySelector('.' + contentClass()).classList.remove('editing');
  const msgs = state.subAgents[instanceName] ? state.subAgents[instanceName].messages : [];
  updateBubbleContent(bubble, msgs[index], config);
}


function deleteMessage(index, instanceName = null) {
  // All agents (including root) now have an instanceName and live in subAgents
  const msgs = state.subAgents[instanceName] ? state.subAgents[instanceName].messages : [];
  const msg = msgs[index];
  if (!msg) return;

  // If deleting an assistant message with a function_call, also delete the function result
  const indicesToDelete = [index];
  if (msg.function_call && index + 1 < msgs.length && msgs[index + 1].role === 'function') {
    indicesToDelete.push(index + 1);
  }
  // If deleting a function result, also delete the preceding function call
  if (msg.role === 'function' && index - 1 >= 0 && msgs[index - 1].function_call) {
    indicesToDelete.push(index - 1);
  }

  send({ type: 'delete_messages', indices: [...new Set(indicesToDelete)], instance_name: instanceName });
}


// ── Approvals ────────────────────────────────────────────────────────────────

function renderApprovals() {
  const bar = approvalBar;

  // Clean up activeSecurityChecks and securityResponses for any IDs that are no longer in state.approvals
  const approvalIds = new Set((state.approvals || []).map(ap => ap.request_id));
  for (const rid of state.activeSecurityChecks) {
    if (!approvalIds.has(rid)) {
      state.activeSecurityChecks.delete(rid);
    }
  }
  for (const rid in state.securityResponses) {
    if (!approvalIds.has(rid)) {
      delete state.securityResponses[rid];
    }
  }

  if (!state.approvals || state.approvals.length === 0) {
    // Approvals are empty: either (a) no pending approvals, or (b) all were auto-applied,
    // or (c) user toggled Auto-Ask off after backend already processed the response
    bar.style.display = 'none';
    return;
  }

  // Auto-security check (Auto-Ask) takes priority
  if (state.autoSecurity) {
    const pending = (state.approvals || []).filter(ap => !state.activeSecurityChecks.has(ap.request_id));
    
    // FIX 3: Only process ONE approval at a time to prevent multiple simultaneous security checks
    // After the first check completes and backend broadcasts updated approvals, 
    // renderApprovals() will be called again and pick up the next pending approval automatically.
    if (pending.length > 0) {
      const ap = pending[0];  // Process only the first pending approval
      state.activeSecurityChecks.add(ap.request_id);
      send({ type: 'ask_security', request_id: ap.request_id, auto_apply: true });
      logger.debug(`[AUTO-ASK] Triggering security check for ${ap.request_id} (1 of ${pending.length} pending)`);
    }
    
    // Don't clear approvals immediately - keep them in case user toggles Auto-Ask off.
    // They will be cleared by the backend when it broadcasts updated approvals after auto-applying.
    bar.style.display = 'none';
    return;
  }

  // Auto-reject if AFK is enabled (and auto-security is OFF)
  if (afkToggle && afkToggle.checked) {
    const reason = (settingAfkMessage && settingAfkMessage.value.trim()) 
      ? `Auto-rejected (AFK): ${settingAfkMessage.value.trim()}`
      : 'Auto-rejected (AFK mode active)';
    
    const pending = [...state.approvals];
    state.approvals = [];
    bar.style.display = 'none';
    
    pending.forEach(ap => {
      send({ type: 'reject', request_id: ap.request_id, reason: reason, automated: true });
    });
    return;
  }

  bar.style.display = 'block';
  bar.innerHTML = '';
  
  // Scroll approval bar into view if it's off-screen (happens with many agent tabs)
  requestAnimationFrame(() => {
    bar.scrollIntoView({ behavior: 'instant', block: 'start' });
  });

  for (const ap of state.approvals) {
    const card = document.createElement('div');
    card.className = 'approval-card';
    card.dataset.requestId = ap.request_id;

    let argsHtml = '';
    try {
      argsHtml = escapeHtml(JSON.stringify(ap.tool_args, null, 2));
    } catch {
      argsHtml = escapeHtml(String(ap.tool_args));
    }

    const isChecking = state.activeSecurityChecks.has(ap.request_id);
    const checkBtnText = isChecking ? '⏳ Checking...' : '🛡️ Ask Security';
    const checkBtnDisabled = isChecking ? 'disabled' : '';

    const secResp = state.securityResponses[ap.request_id];
    let securityHtml = '';
    if (secResp) {
       securityHtml = `<div class="security-response-box">
         <strong>🛡️ Security Expert:</strong><div style="margin-top:4px;">${renderMarkdown(secResp.response)}</div>
       </div>`;
    }

    // Extract justification from the dedicated field. The fallback to tool_args is for
    // backward compat with pre-change approval objects still in-flight during deployment.
    let justification = '';
    if (ap.justification && ap.justification.trim()) {
      justification = escapeHtml(ap.justification.trim());
    } else if (ap.tool_args && ap.tool_args.justification && ap.tool_args.justification.trim()) {
      justification = escapeHtml(ap.tool_args.justification.trim());
    }

    // Build the justification block — most prominent info in the card
    let justificationHtml = '';
    if (justification) {
      justificationHtml = `<div class="approval-justification">${justification}</div>`;
    }

    // Tool icon based on tool name for visual clarity
    const toolIconMap = {
      'write_file': '📝', 'read_file': '📄', 'edit_file': '✏️',
      'shell_cmd': '⚙️', 'call_agent': '🤖', 'delete_file': '🗑️',
      'copy_file': '📋', 'move_file': '🔀', 'grep': '🔍', 'list_dir': '📁',
      'view_image': '🖼️', 'code_map': '🗺️', 'syntax_check': '✅',
      'web_extractor': '🌐', 'ddg_search': '🔎', 'code_interpreter': '🐍',
    };
    const toolIcon = toolIconMap[ap.tool_name] || '🛠️';

    card.innerHTML = `
      <div class="approval-header">
        <span class="approval-icon">${toolIcon}</span>
        <strong>${escapeHtml(ap.tool_name)}</strong>
        <span class="approval-agent-label">by ${escapeHtml(ap.agent_name)}</span>
      </div>
      ${justificationHtml}
      <pre class="approval-args-block">${argsHtml}</pre>
      ${securityHtml}
      <div class="approval-actions">
        <button class="btn btn-primary btn-sm" data-request-id="${escapeHtml(ap.request_id)}" onclick="approveRequest(this.dataset.requestId)">✅ Approve</button>
        <button class="btn btn-warning btn-sm ask-security-btn" ${checkBtnDisabled} data-request-id="${escapeHtml(ap.request_id)}" onclick="askSecurity(this.dataset.requestId, this)">${checkBtnText}</button>
        <button class="btn btn-danger btn-sm" data-request-id="${escapeHtml(ap.request_id)}" onclick="showRejectInput(this.dataset.requestId, this)">❌ Reject</button>
      </div>
    `;
    bar.appendChild(card);
  }
}

// Global functions for inline onclick handlers
window.approveRequest = function (requestId) {
  send({ type: 'approve', request_id: requestId });
};

window.askSecurity = function (requestId, btn) {
  state.activeSecurityChecks.add(requestId);
  delete state.securityResponses[requestId];
  const originalHtml = btn.innerHTML;
  btn.innerHTML = '⏳ Checking...';
  btn.disabled = true;
  send({ type: 'ask_security', request_id: requestId, auto_apply: false });
};

window.showRejectInput = function (requestId, btn) {
  const card = btn.closest('.approval-card');
  const existing = card.querySelector('.reject-input-area');
  if (existing) { existing.remove(); return; }

  const area = document.createElement('div');
  area.className = 'reject-input-area';
  area.innerHTML = `
    <input type="text" placeholder="Rejection reason..." class="reject-reason-input" id="reject-${escapeHtml(requestId)}">
    <button class="btn btn-danger btn-sm" data-request-id="${escapeHtml(requestId)}" onclick="rejectRequest(this.dataset.requestId)">Confirm Reject</button>
  `;
  card.appendChild(area);
  area.querySelector('input').focus();
};

window.rejectRequest = function (requestId) {
  const input = document.getElementById(`reject-${requestId}`);
  const reason = input ? input.value.trim() : 'Rejected by user';
  send({ type: 'reject', request_id: requestId, reason: reason || 'Rejected by user' });
};

// ── Sub-agents ───────────────────────────────────────────────────────────────

function renderSubAgents() {
  const t0 = performance.now();
  
  // Preserve chat input focus and cursor position during DOM manipulation.
  // renderSubAgents rebuilds message panels which can steal focus from #chatInput,
  // causing the caret to jump while typing or during streaming.
  const wasFocused = document.activeElement === chatInput;
  let savedSelStart = null;
  let savedSelEnd = null;
  if (wasFocused) {
    savedSelStart = chatInput.selectionStart;
    savedSelEnd = chatInput.selectionEnd;
  }

  const sa = state.subAgents;
  
  // Build agent list from subAgents, filtered by closedTabs.
  // All agents are equal — no root-first sorting needed.
  const namesArr = Object.keys(sa).filter(name => !state.closedTabs.has('sub-' + name));

  // Remove stale sub-agent tabs and panels for agents that no longer exist
  mainTabBar.querySelectorAll('.main-tab[data-tab^="sub-"]').forEach(tab => {
    const agentName = tab.dataset.tab.substring(4);
    if (!namesArr.includes(agentName)) {
      tab.remove();
      const panel = document.getElementById('panelSub-' + agentName);
      if (panel) panel.remove();
      // Clean up per-panel state when agent is removed
      delete subAgentScrollLocks[agentName];
    }
  });

  if (namesArr.length === 0) return;

  // Auto-select active tab from stack
  const activeTop = state.activeStack.length > 0 ? state.activeStack[state.activeStack.length - 1] : null;

  for (const name of namesArr) {
    const tabId = 'sub-' + name;
    // Check if this is the session primary agent (was "root")
    const isSessionPrimary = isSessionPrimaryAgent(name);
    const agentData = sa[name];
    // Agent-specific active state only — no global fallback (prevents cross-agent pulsing)
    const isActive = agentData?.active ?? false;

    // Create tab button if it doesn't exist
    let tabBtn = mainTabBar.querySelector(`.main-tab[data-tab="${tabId}"]`);
    if (!tabBtn) {
      tabBtn = document.createElement('button');
      tabBtn.className = 'main-tab';
      tabBtn.dataset.tab = tabId;
      tabBtn.onclick = () => switchMainTab(tabId);

      const iconSpan = document.createElement('span');
      iconSpan.className = 'tab-icon-container';
      tabBtn.appendChild(iconSpan);

      const labelSpan = document.createElement('span');
      labelSpan.className = 'tab-label';
      tabBtn.appendChild(labelSpan);

      if (!isSessionPrimary) {
        const closeBtn = document.createElement('span');
        closeBtn.className = 'close-tab';
        closeBtn.title = 'Close Agent';
        closeBtn.textContent = '\u00d7';
        closeBtn.onclick = (e) => {
          e.stopPropagation();
          send({ type: 'terminate_agent_instance', instance_name: name });
          switchMainTab(getAgentTabId(state.sessionName));
        };
        tabBtn.appendChild(closeBtn);
      }

      mainTabBar.appendChild(tabBtn);
    }

    // Update tab content safely (preserves handlers on closeBtn)
    const iconSpan = tabBtn.querySelector('.tab-icon-container');
    if (iconSpan) {
      // Get agent state for visibility logic
      const agentState = agentData?.agent_state || 'idle';
      // Show indicator for RUNNING or SLEEPING states (agent is actively doing something)
      const shouldShowIndicator = isActive || agentState === 'SLEEPING';
      
      // Only update icon innerHTML when active state actually changed to avoid GPU churn
      const prevActive = tabBtn.dataset.isActive === 'true';
      if (prevActive !== shouldShowIndicator) {
        const icon = agentData?.agent_class === 'orchestrator' ? '💬' : '🤖';
        iconSpan.innerHTML = shouldShowIndicator 
          ? '<span class="sub-tab-pulse"></span> ' + `<span class="main-tab-icon">${icon}</span>` 
          : `<span class="main-tab-icon">${icon}</span>`;
      }
      tabBtn.dataset.isActive = String(shouldShowIndicator);
    }
    
    // Update agent state class for colored activity indicator (needed for CSS selectors)
    const agentStateForClass = agentData?.agent_state || 'idle';
    const prevStateClass = tabBtn.dataset.agentState;
    if (prevStateClass !== agentStateForClass) {
      // Remove old state classes
      tabBtn.classList.remove('state-running', 'state-sleeping', 'state-idle', 'state-completing', 'state-terminated');
      // Add new state class
      const stateClass = 'state-' + agentStateForClass.toLowerCase();
      tabBtn.classList.add(stateClass);
      tabBtn.dataset.agentState = agentStateForClass;
    }
    
    const labelSpan = tabBtn.querySelector('.tab-label');
    if (labelSpan && labelSpan.textContent !== ` ${name}`) {
      labelSpan.textContent = ` ${name}`;
    }

    // Highlight the active sub-agent's tab
    if (isActive) {
      tabBtn.classList.add('agent-active');
      if (activeTop === name) {
        tabBtn.classList.add('has-activity');
      } else {
        tabBtn.classList.remove('has-activity');
      }
    } else {
      tabBtn.classList.remove('agent-active');
      tabBtn.classList.remove('has-activity');
    }

    // Create or update panel
    let panel = document.getElementById('panelSub-' + name);
    if (!panel) {
      panel = document.createElement('div');
      panel.className = 'main-tab-panel sub-agent-panel';
      panel.id = 'panelSub-' + name;

      const contextBar = document.createElement('div');
      contextBar.className = 'context-bar';
      contextBar.title = 'Context Usage';
      const contextFill = document.createElement('div');
      contextFill.className = 'context-bar-fill';
      contextFill.id = 'subContextFill-' + name;
      contextBar.appendChild(contextFill);
      panel.appendChild(contextBar);

      mainTabPanels.appendChild(panel);
    }

    // Render sub-agent messages into the panel
    renderSubAgentPanel(panel, sa[name], name);
  }

  // Restore chat input focus and cursor position if user was typing during render
  if (wasFocused && savedSelStart !== null) {
    chatInput.selectionStart = savedSelStart;
    chatInput.selectionEnd = savedSelEnd;
    if (document.activeElement !== chatInput) {
      chatInput.focus();
    }
  }
  
  // Diagnostic: warn on slow renders
  const renderMs = performance.now() - t0;
  if (renderMs > THROTTLE.RENDER_DIAG_THRESHOLD_MS) {
    console.warn(`[renderSubAgents] took ${renderMs.toFixed(1)}ms for ${namesArr.length} agents`);
  }
}

function renderSubAgentPanel(panel, agentData, name) {
  const isVisible = state.activeSubTab === 'sub-' + name;
  const msgs = (agentData && agentData.messages) ? agentData.messages : [];
  
  // Agent-specific active state only — no global fallback (prevents cross-agent pulsing)
  const isActive = agentData?.active ?? false;
  
  // Unified token/word counts: use agent-level stats if available, fall back to global
  const tokCount = agentData?.total_tokens ?? state.totalTokens;
  const wordCount = agentData?.total_words ?? state.totalWords;
  const maxTok = agentData?.max_tokens ?? state.maxTokens;
  
  // Pool mirror: show ALL messages including system prompt — no filtering
  const displayMsgs = msgs;
  const lastMsg = displayMsgs.length > 0 ? displayMsgs[displayMsgs.length - 1] : null;

  // 1. Ensure basic structure exists (once)
  if (!panel.dataset.initialized) {
    panel.dataset.initialized = "true";
    
    // Scroll Container — unified class matches root chat panel
    const scrollContainer = document.createElement('div');
    scrollContainer.className = 'messages messages-scroll';
    panel.appendChild(scrollContainer);
    
    // Reset scroll lock state for fresh panel (prevents stale listenerAdded on session reset)
    subAgentScrollLocks[name] = { locked: true, listenerAdded: false };
  }

  const scrollContainer = panel.querySelector('.messages');
  
  // Set up per-panel scroll lock state and listener (runs every call but guarded by listenerAdded).
  // Placed OUTSIDE the dataset.initialized guard so that if a stream_update fires before
  // initialization completes, the scroll listener is still attached — prevents Bug #1
  // where scrolling decoupling breaks on newly-created panels.
  if (!subAgentScrollLocks[name]) {
    subAgentScrollLocks[name] = { locked: true, listenerAdded: false }; // Start locked at bottom
  }
  if (!subAgentScrollLocks[name].listenerAdded && scrollContainer) {
    scrollContainer.addEventListener('scroll', () => {
      const distFromBottom = scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight;
      subAgentScrollLocks[name].locked = (distFromBottom < 50);
    });
    subAgentScrollLocks[name].listenerAdded = true;
  }
  
  // Update global activity bar if this is the active visible tab
  if (isVisible) {
    ActivityBar.render();
  }

  // 3. LAZY RENDERING: Skip expensive message work if the tab isn't visible
  if (!isVisible) {
    // Still update status bar stats even when hidden (was in old renderMessages)
    if (statusWords) statusWords.textContent = `${wordCount} words`;
    if (statusTokens) statusTokens.textContent = `${tokCount} tokens`;
    return;
  }

  // Update status bar stats
  if (statusWords) statusWords.textContent = `${wordCount} words`;
  if (statusTokens) statusTokens.textContent = `${tokCount} tokens`;

  // 4. Check Content Key EARLY to skip all layout reads and DOM work when nothing changed
  // Optimized: use .length directly on strings (no String() wrapper needed for known-string values)
  // and short-circuit early checks before expensive operations
  const lastMsgTextLen = lastMsg ? (Array.isArray(lastMsg.content)
    ? lastMsg.content.reduce((s, item) => s + (item.text?.length || 0), 0)
    : (lastMsg.content || '').length) : 0;
  const reasoningLen = lastMsg ? (lastMsg.reasoning_content || '').length : 0;
  const funcCallLen = lastMsg?.function_call?.arguments ? (lastMsg.function_call.arguments + '').length : 0;
  // Agent-specific active flag only — no global fallback (prevents cross-agent pulsing)
  const activeFlag = agentData?.active ?? false;
  
  // Include token count in contentKey so status bar updates even when text length is unchanged.
  // During tool execution pauses, text doesn't grow but tokens do — without this the render skips.
  const tokPart = (typeof tokCount === 'number' && !isNaN(tokCount)) ? tokCount : '0';
  
  // Include streaming state in contentKey so bubble UI (waiting animation, token count) updates even when content dims match
  const contentKey = displayMsgs.length + ':' + lastMsgTextLen + ':' + reasoningLen + ':' + funcCallLen + ':' + activeFlag + ':' + (state.generating ? '1' : '0') + ':' + tokPart;
  
  if (panel.dataset.contentKey === contentKey && state.editingIndex === null && parseInt(panel.dataset.lastRenderedCount || '0') === displayMsgs.length) {
    // Nothing changed — skip scrollHeight read and all DOM updates.
    // BUT during active streaming, force at least one render per throttle cycle to catch
    // incremental append drift (raw text appended via updateBubbleContent may diverge from
    // markdown-structured state). The incrementCount resets every 8 ticks, so we use that
    // as a freshness signal — if generating and no new messages, still validate once.
    if (!state.generating) {
      return;
    }
    // During streaming: always update the bubble content to ensure incremental appends are flushed.
    // This is lightweight (updateBubbleContent has its own prev-content check).
  }

  panel.dataset.contentKey = contentKey;

  // 6. Incremental Rendering
  const currentCount = displayMsgs.length;
  const lastCount = parseInt(panel.dataset.lastRenderedCount || '0');

  if (currentCount < lastCount || lastCount === 0) {
    scrollContainer.innerHTML = '';
    // Pass isGenerating via config override so all messages know the streaming state
    const subConfig = getAgentConfig(name);
    subConfig.isGenerating = isActive;
    scrollContainer.appendChild(renderAgentConversation(name, displayMsgs, 1, null, subConfig));
    
    // Show a loading placeholder when agent is active but has no messages yet
    if (currentCount === 0 && isActive) {
      const loadingDiv = document.createElement('div');
      loadingDiv.className = 'message msg-system';
      loadingDiv.dataset.placeholder = 'initializing';
      loadingDiv.innerHTML = '<div class="msg-content">⏳ Initializing…</div>';
      scrollContainer.appendChild(loadingDiv);
    }
    
    // Update context bar with unified token counts
    updateContextBar(document.getElementById('subContextFill-' + name), displayMsgs, tokCount, maxTok);
  } else {
    // CRITICAL: Verify DOM actually contains lastCount child elements before appending.
    // If the container was cleared by another path (tab switch, retry, etc.) but
    // lastRenderedCount wasn't reset in time, appending would create duplicates.
    const actualChildCount = scrollContainer.children.length;
    if (actualChildCount !== lastCount) {
      // DOM is out of sync — do a full re-render instead of append
      scrollContainer.innerHTML = '';
      const subConfig = getAgentConfig(name);
      subConfig.isGenerating = isActive;
      scrollContainer.appendChild(renderAgentConversation(name, displayMsgs, 1, null, subConfig));
      
      // Show a loading placeholder when agent is active but has no messages yet
      if (currentCount === 0 && isActive) {
        const loadingDiv = document.createElement('div');
        loadingDiv.className = 'message msg-system';
        loadingDiv.dataset.placeholder = 'initializing';
        loadingDiv.innerHTML = '<div class="msg-content">⏳ Initializing…</div>';
        scrollContainer.appendChild(loadingDiv);
      }
      
      // Update context bar since we just did a full re-render
      updateContextBar(document.getElementById('subContextFill-' + name), displayMsgs, tokCount, maxTok);
    } else {
      // Safe to append — DOM matches expected state
      // Remove loading placeholder if it exists (messages have arrived)
      if (currentCount > 0) {
        const existingPlaceholder = scrollContainer.querySelector('[data-placeholder="initializing"]');
        if (existingPlaceholder) {
          existingPlaceholder.remove();
        }
      }

      // Feature Plan #021: Before adding new messages, ensure the PREVIOUS last message 
      // gets one final full render to ensure it's fully formatted (e.g. reasoning block finished).
      if (currentCount > lastCount && scrollContainer.lastElementChild) {
        const subConfig = getAgentConfig(name);
        // The previous message is by definition not the "actively generating" one anymore
        // if a new message has arrived.
        subConfig.isGenerating = false; 
        updateBubbleContent(scrollContainer.lastElementChild, displayMsgs[lastCount - 1], subConfig);
      }

      const newMsgs = [];
      for (let i = lastCount; i < currentCount; i++) {
        newMsgs.push(displayMsgs[i]);
      }

      // OPTIMIZATION #2: Use direct DOM append for single messages to avoid DocumentFragment overhead.
      // For streaming deltas (commonly 1 message), direct append is faster than building a fragment.
      if (newMsgs.length === 1) {
        const subConfig = getAgentConfig(name);
        subConfig.isGenerating = isActive;
        const msgEl = createMessageEl(newMsgs[0], lastCount, subConfig);
        scrollContainer.appendChild(msgEl);
      } else {
        // Multiple messages — use DocumentFragment for efficiency
        const newIndexMap = [];
        for (let i = lastCount; i < currentCount; i++) {
          newIndexMap.push(i);
        }
        const subConfig = getAgentConfig(name);
        subConfig.isGenerating = isActive;
        scrollContainer.appendChild(renderAgentConversation(name, newMsgs, 1, newIndexMap, subConfig));
      }

      // OPTIMIZATION #4: Update context bar unconditionally (actual work is cheap - just updating a progress bar).
      // Removed per-agent throttling to reduce blocking during render cycle.
      const fillEl = document.getElementById('subContextFill-' + name);
      if (fillEl) {
        updateContextBar(fillEl, displayMsgs, tokCount, maxTok);
      }
    }

    // Use unified bubble content update with isGenerating passed via config
    if (scrollContainer.lastElementChild) {
      const subConfig = getAgentConfig(name);
      subConfig.isGenerating = isActive;
      updateBubbleContent(scrollContainer.lastElementChild, displayMsgs[currentCount - 1], subConfig);
    }
  }
  panel.dataset.lastRenderedCount = currentCount;

  // Step 8: Unified auto-scroll using requestAnimationFrame and per-panel scroll lock state
  requestAnimationFrame(() => {
    const atBottom = scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight < 50;
    
    if (subAgentScrollLocks[name]?.locked) {
      scrollContainer.scrollTop = scrollContainer.scrollHeight; // Scroll — scroll listener handles lock state
    } else if (atBottom && subAgentScrollLocks[name]) {
      subAgentScrollLocks[name].locked = true; // Re-lock if user scrolled back to bottom
    }
  });
}

function switchMainTab(tabId) {
  // Update tab buttons
  mainTabBar.querySelectorAll('.main-tab').forEach(t => t.classList.remove('active'));
  const activeTab = mainTabBar.querySelector(`.main-tab[data-tab="${tabId}"]`);
  if (activeTab) activeTab.classList.add('active');

  // Update panels — all tabs use the same dynamic panel system now
  mainTabPanels.querySelectorAll('.main-tab-panel').forEach(p => p.classList.remove('active'));
  
  const name = tabId.substring(4); // strip 'sub-' prefix (works for root too: sub-Maine → Maine)
  const panel = document.getElementById('panelSub-' + name);
  if (panel) {
    panel.classList.add('active');
    const scroll = panel.querySelector('.messages');
    if (scroll) {
      // Only scroll to bottom if user was near the bottom or agent is actively generating
      const distFromBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight;
      const isGenerating = state.subAgents[name]?.active || state.generating;
      if (distFromBottom < 50 || isGenerating) {
        scroll.scrollTop = scroll.scrollHeight;
      }
    }
  }
  
  state.activeSubTab = tabId;
  ActivityBar.setActiveTab(tabId);
  
  // Invalidate target panel's content key cache to force a full re-render.
  // Without this, if the agent didn't receive new messages while hidden,
  // the content key matches and ALL rendering is skipped (including activity bar
  // updates, context bar updates, and status bar updates). This ensures the
  // visible tab always gets a proper render on tab switch.
  if (panel) {
    delete panel.dataset.contentKey;
    delete panel.dataset.lastRenderedCount;
  }
  
  // Reset throttle timer so the new tab renders immediately
  state.genStats.lastSubAgentRender = 0;
  
  renderSubAgents();
  
  // Ensure active class is set on tab and panel after renderSubAgents()
  // (When switching to a brand-new agent, the tab/panel didn't exist when
  // we tried to add 'active' above — re-apply now that they've been created.)
  const targetTab = mainTabBar.querySelector(`.main-tab[data-tab="${tabId}"]`);
  if (targetTab) targetTab.classList.add('active');
  // Re-query in case panel was just created by renderSubAgents() for a new agent
  const finalPanel = document.getElementById('panelSub-' + name);
  if (finalPanel) finalPanel.classList.add('active');
}

// Root tab is now created dynamically via renderSubAgents() — no static wiring needed

// ── Agent selector ───────────────────────────────────────────────────────────

const settingAgentSelect = $('#setting-agent-select');
const settingToolsList = $('#setting-tools-list');

if (!localStorage.getItem('agent-cascade-tools-migrated-v2')) {
  localStorage.removeItem('agent-cascade-disabled-tools');
  localStorage.setItem('agent-cascade-tools-migrated-v2', '1');
}

let agentDisabledTools = JSON.parse(localStorage.getItem('agent-cascade-disabled-tools') || localStorage.getItem('qwen-disabled-tools') || '{}');

function renderAgentSelect() {
  if (agentSelect) agentSelect.innerHTML = '';
  if (settingAgentSelect) settingAgentSelect.innerHTML = '';

  let updatedDisabledTools = false;

  for (const agent of state.agents) {
    if (!agentDisabledTools[agent.name] && agent.tools) {
      const defaultTools = agent.default_tools || agent.tools;
      agentDisabledTools[agent.name] = agent.tools.filter(t => !defaultTools.includes(t));
      updatedDisabledTools = true;
    }

    if (agentSelect) {
      const opt = document.createElement('option');
      opt.value = agent.index;
      opt.textContent = agent.name;
      if (agent.index === state.agentIndex) opt.selected = true;
      agentSelect.appendChild(opt);
    }
    if (settingAgentSelect) {
      const opt2 = document.createElement('option');
      opt2.value = agent.index;
      opt2.textContent = agent.name;
      if (agent.index === state.viewingAgentIndex) opt2.selected = true;
      settingAgentSelect.appendChild(opt2);
    }
  }

  if (updatedDisabledTools) {
    localStorage.setItem('agent-cascade-disabled-tools', JSON.stringify(agentDisabledTools));
  }

  renderToolsForSelectedAgent();
}

function renderToolsForSelectedAgent() {
  if (!settingToolsList || !settingAgentSelect) return;
  const idx = parseInt(settingAgentSelect.value);
  if (isNaN(idx)) return;
  const agent = state.agents.find(a => a.index === idx);

  if (!agent || !agent.tools || agent.tools.length === 0) {
    settingToolsList.innerHTML = '<div style="color: var(--text-muted); font-size: 12px;">No tools available for this agent.</div>';
    return;
  }

  const disabled = agentDisabledTools[agent.name] || [];

  settingToolsList.innerHTML = agent.tools.map(toolName => `
    <label class="setting-field toggle-field">
      <span>${escapeHtml(toolName)}</span>
      <input type="checkbox" class="tool-toggle" data-agent="${escapeHtml(agent.name)}" data-tool="${escapeHtml(toolName)}" ${!disabled.includes(toolName) ? 'checked' : ''} />
    </label>
  `).join('');

  // ── Event Delegation (single listener on container, no per-checkbox leaks) ──
  if (!toolsDelegationAttached) {
    toolsDelegationAttached = true;
    settingToolsList.addEventListener('change', handleToolToggleChange);
  }
}

// ── Delegated event handler for tool toggles ────────────────────────────────
let toolsDelegationAttached = false; // Guard against accumulating listeners on settingToolsList

function handleToolToggleChange(e) {
  const chk = e.target.closest('.tool-toggle');
  if (!chk) return;
  const aName = chk.dataset.agent;
  const tName = chk.dataset.tool;
  if (!agentDisabledTools[aName]) agentDisabledTools[aName] = [];
  if (!e.target.checked) {
    if (!agentDisabledTools[aName].includes(tName)) agentDisabledTools[aName].push(tName);
  } else {
    agentDisabledTools[aName] = agentDisabledTools[aName].filter(t => t !== tName);
  }
  localStorage.setItem('agent-cascade-disabled-tools', JSON.stringify(agentDisabledTools));
  saveSettings();
}

if (settingAgentSelect) {
  settingAgentSelect.addEventListener('change', (e) => {
    state.viewingAgentIndex = parseInt(e.target.value);
    renderToolsForSelectedAgent();
  });
}

// Settings Tabs
document.querySelectorAll('.settings-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.settings-tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    const panel = document.getElementById(btn.dataset.tab);
    if (panel) panel.classList.add('active');
  });
});

// Sub-tabs within Agent & Tools panel (System vs Per Agent)
document.querySelectorAll('.settings-sub-tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.settings-sub-tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.settings-sub-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    // Use data-target attribute to explicitly link button to its panel (more maintainable than string concatenation)
    const targetPanel = document.getElementById(btn.dataset.target);
    if (targetPanel) targetPanel.classList.add('active');
  });
});

// ── Controls ─────────────────────────────────────────────────────────────────

function updateControls() {
  const isGenerating = state.generating;
  
  // Only do destructive innerHTML/body classList changes when generating state actually changes
  if (state._lastIsGenerating !== isGenerating) {
    if (isGenerating) {
      sendBtn.classList.add('inject-mode');
      sendBtn.title = 'Inject message into active agent (Enter)';
      // Update the active generating agent's tab to show pulse indicator immediately.
      // renderSubAgents() also handles icon updates but is throttled (~200ms); this provides instant visual feedback when generation starts/stops.
      const activeAgentName = getActiveAgentName();
      const activeTabEl = mainTabBar.querySelector(`.main-tab[data-tab="${getAgentTabId(activeAgentName)}"]`);
      if (activeTabEl) {
        const icon = state.subAgents[activeAgentName]?.agent_class === 'orchestrator' ? '💬' : '🤖';
        activeTabEl.innerHTML = '<span class="sub-tab-pulse"></span> ' + escapeHtml(activeAgentName || DEFAULT_SESSION_NAME);
      }
      resetBtn.disabled = true;
      document.body.classList.add('is-generating');
    } else {
      sendBtn.classList.remove('inject-mode');
      sendBtn.title = 'Send (Enter)';
      // Restore active agent tab icon
      const activeAgentName = getActiveAgentName();
      const activeTabEl = mainTabBar.querySelector(`.main-tab[data-tab="${getAgentTabId(activeAgentName)}"]`);
      if (activeTabEl) {
        const icon = state.subAgents[activeAgentName]?.agent_class === 'orchestrator' ? '💬' : '🤖';
        activeTabEl.innerHTML = '<span class="main-tab-icon">' + icon + '</span> ' + escapeHtml(activeAgentName || DEFAULT_SESSION_NAME);
      }
      resetBtn.disabled = false;
      document.body.classList.remove('is-generating');
    }
    state._lastIsGenerating = isGenerating;
  }
  if (stopBtn) stopBtn.style.opacity = state.generating ? '1' : '0.4';
  sendBtn.disabled = !state.connected;
  const activeMsgs = state.subAgents[getActiveAgentName()]?.messages || [];
  continueBtn.disabled = state.generating || activeMsgs.length === 0;
  const refreshBtn = document.getElementById('refreshBtn');
  const mainRB = document.getElementById('mainRetryBtn');
  const retryDisabled = state.generating || activeMsgs.length === 0;
  if (refreshBtn) refreshBtn.disabled = state.generating;
  if (mainRB) mainRB.disabled = retryDisabled;

  const activeInstance = getActiveInstanceName();
  const activeAgentData = state.subAgents[activeInstance];
  const isHalted = !!activeAgentData?.is_halted;

  // Check if ANY active agent is halted (not just the current one) for accurate status display
  const anyHalted = Object.values(state.subAgents).some(a => a?.is_halted);

  // Show halt status even during generation — prevents misleading "Generating..." when something is stuck
  statusText.textContent = anyHalted ? '⏸ Paused' : (state.generating ? 'Generating...' : '');
  
  // Sync pause button to current active agent's halted state
  if (pauseBtn) {
    pauseBtn.textContent = isHalted ? '▶️ Resume' : '⏸ Pause';
  }

  // Sync terminate button visibility — unified active check
  const terminateBtn = document.getElementById('terminateBtn');
  if (terminateBtn) {
    const isInstanceActive = activeAgentData?.active ?? state.generating;
    terminateBtn.style.display = isInstanceActive ? 'inline-flex' : 'none';
  }
  
  chatInput.placeholder = state.generating
    ? 'Inject a message into the active agent...'
    : 'Send a message...';

  if (sessionNameInput && document.activeElement !== sessionNameInput) {
    sessionNameInput.value = state.sessionName;
  }
}

// ── Auto-resize textarea ─────────────────────────────────────────────────────

function autoResize(el) {
  if (!el) return;

  // Preserve cursor position before any layout-changing operations.
  // Setting height='auto' can shift the line the caret is on, causing it to jump.
  const selStart = el.selectionStart;
  const selEnd = el.selectionEnd;

  // Always assign explicit pixel height — even if unchanged, this prevents
  // latent bugs where the textarea ends up with 'auto' and inconsistent sizing.
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 200) + 'px';

  // Restore cursor position after resize completes.
  el.selectionStart = selStart;
  el.selectionEnd = selEnd;
}

function estimateTokens(text) {
  if (!text) return 0;
  let imageTokens = 0;

  // 1. Detect and strip base64 image patterns (markdown format)
  // Handles both standard data: URIs and raw base64 (common in tool results)
  const imageRegex = /!\[(.*?)\]\((?:data:image\/[^;]+;base64,)?[a-zA-Z0-9+/=]{50,}\)/g;

  // 2. Also catch raw large base64 blobs not in markdown format (e.g. raw tool outputs)
  const rawBlobRegex = /(?:data:image\/[^;]+;base64,)?[a-zA-Z0-9+/=]{500,}/g;

  const visionEnabled = (typeof settingVisionEnabled !== 'undefined' && settingVisionEnabled) ? settingVisionEnabled.checked : false;

  let cleanedText = text.replace(imageRegex, (match, alt) => {
    if (visionEnabled) imageTokens += 255;
    return `[Image: ${alt}]`;
  });

  cleanedText = cleanedText.replace(rawBlobRegex, () => {
    // If it's a huge raw blob, we still treat it as a potential image/data block
    if (visionEnabled) imageTokens += 255;
    return '[DATA BLOB]';
  });

  // Prose estimation: average 1 token ≈ 4.86 characters
  return Math.ceil(cleanedText.length / 4.86) + imageTokens;
}

/**
 * Format token count with K/M suffixes for compact display.
 * @param {number} count - Token count to format
 * @returns {string} Formatted string (e.g., "1K", "1.5K", "2.3M")
 */
function formatTokenCount(count) {
  // Defensive: handle negative or NaN values
  if (count < 0 || isNaN(count)) count = 0;
  
  if (count >= 1000000) {
    return (count / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
  } else if (count >= 1000) {
    return (count / 1000).toFixed(1).replace(/\.0$/, '') + 'K';
  }
  return String(count);
}

/**
 * Update the context bar fill and numeric counter display for an agent tab.
 * @param {HTMLElement} barEl - The .context-bar-fill element to update
 * @param {Array} msgs - Message array (unused but kept for API compatibility)
 * @param {number} overrideTokens - Token count to use, or falls back to cached value
 * @param {number} overrideMax - Max token context limit from backend
 */
function updateContextBar(barEl, msgs, overrideTokens, overrideMax) {
  if (!barEl) return;

  const tokens = overrideTokens || 0;
  // Prioritize the per-agent max_tokens from the backend (actual model context limit).
  // Fall back to the UI setting only if the backend didn't provide a value, then to a hardcoded default.
  const maxContext = (overrideMax && overrideMax > 0) ? overrideMax :
                     ((settingMaxContext && settingMaxContext.value) ? parseInt(settingMaxContext.value) : 32768);

  const pct = Math.min(100, Math.max(0, (tokens / maxContext) * 100));
  barEl.style.width = pct + '%';
  
  // Update tooltip with full numeric values
  barEl.title = `${tokens} / ${maxContext} tokens`;

  if (pct > 90) {
    barEl.className = 'context-bar-fill danger';
  } else if (pct > 75) {
    barEl.className = 'context-bar-fill warning';
  } else {
    barEl.className = 'context-bar-fill';
  }

  // Update or create the numeric counter display on the right side of the context bar
  const contextBarContainer = barEl.parentElement;
  if (contextBarContainer) {
    let counterEl = contextBarContainer.querySelector('.context-bar-counter');
    if (!counterEl) {
      counterEl = document.createElement('span');
      counterEl.className = 'context-bar-counter';
      contextBarContainer.appendChild(counterEl);
    }
    // Display percentage: show at least 1% for any non-zero token usage
    const pctDisplay = Math.round(pct) || (tokens > 0 ? 1 : 0);
    // Display: "used / max (percentage%)" with formatted token counts
    counterEl.textContent = `${formatTokenCount(tokens)} / ${formatTokenCount(maxContext)} (${pctDisplay}%)`;
  }
}

function updateAllContextBars() {
  const sa = state.subAgents;
  for (const name of Object.keys(sa)) {
    const fillEl = document.getElementById('subContextFill-' + name);
    if (fillEl) {
      const agentData = sa[name];
      const displayMsgs = agentData?.messages || [];
      // Unified token counts: use agent-level stats if available, fall back to global
      const tokCount = agentData?.total_tokens ?? state.totalTokens;
      const maxTok = agentData?.max_tokens ?? state.maxTokens;
      updateContextBar(fillEl, displayMsgs, tokCount, maxTok);
    }
  }
}

function resetGenStats() {
  const saStartCounts = {};
  if (state.subAgents) {
    for (const name in state.subAgents) {
      saStartCounts[name] = (state.subAgents[name].messages || []).length;
    }
  }

  state.genStats = {
    startTime: performance.now(),
    firstTokenTime: 0,
    lastTokenTime: 0,
    activeGenTime: 0,
    startMsgCount: (state.subAgents[getActiveAgentName()]?.messages || []).length,
    measuredAgent: getActiveAgentName(), // Track which agent's msgs are passed as main param
    saStartCounts: saStartCounts,
    tokenCount: 0,
    active: true,
    // Reset throttle timestamps at generation start for fresh timing windows
    lastGenStatsUpdate: 0,
    lastSubAgentRender: 0,
    lastSubAgentRenderDuration: 0,
    lastContextBarUpdate: 0,
    lastUiUpdate: 0,
    lastControlsUpdate: 0,
    lastTelemetryUpdate: 0,
  };
  if (statusTokensSec) statusTokensSec.textContent = '— t/s';
  if (statusGenInfo) statusGenInfo.textContent = 'Starting...';
}

function updateGenStats(msgs, isFinal = false) {
  if (!state.genStats.active) return;
  if (!statusTokensSec || !statusGenInfo) return;

  // Extremely lightweight token calculation: only look at new messages in this turn
  // and use basic length division (O(1) string length reads) instead of regex
  let currentGenLength = 0;
  
  // 1. Main Agent Tokens
  const startIdx = state.genStats.startMsgCount || 0;
  for (let i = startIdx; i < msgs.length; i++) {
    const m = msgs[i];
    if (m.role !== 'user' && m.role !== 'function') {
      currentGenLength += (m.content || '').length + (m.reasoning_content || '').length;
      if (m.function_call) {
        currentGenLength += (m.function_call.name || '').length + (typeof m.function_call.arguments === 'string' ? m.function_call.arguments.length : JSON.stringify(m.function_call.arguments || '').length);
      }
    }
  }

  // 2. Sub-Agent Tokens (exclude the agent whose msgs were passed as main param)
  const measuredAgent = state.genStats.measuredAgent || state.sessionName;
  if (state.subAgents) {
    for (const name in state.subAgents) {
      if (name === measuredAgent) continue; // Agent handled via msgs param above
      const saMsgs = state.subAgents[name].messages || [];
      const saStart = (state.genStats.saStartCounts && state.genStats.saStartCounts[name]) || 0;
      for (let i = saStart; i < saMsgs.length; i++) {
        const m = saMsgs[i];
        if (m.role !== 'user' && m.role !== 'function') {
          currentGenLength += (m.content || '').length + (m.reasoning_content || '').length;
          if (m.function_call) {
            currentGenLength += (m.function_call.name || '').length + (typeof m.function_call.arguments === 'string' ? m.function_call.arguments.length : JSON.stringify(m.function_call.arguments || '').length);
          }
        }
      }
    }
  }

  const currentGenTokens = Math.ceil(currentGenLength / 3.5);
  const now = performance.now();
  
  if (currentGenTokens > state.genStats.tokenCount) {
    if (state.genStats.firstTokenTime === 0) {
      state.genStats.firstTokenTime = now;
      state.genStats.lastTokenTime = now;
      state.genStats.activeGenTime = 0;
    } else {
      const delta = now - state.genStats.lastTokenTime;
      // Cap the time addition to avoid destroying TPS during tool execution pauses
      state.genStats.activeGenTime += Math.min(delta, 2000);
      state.genStats.lastTokenTime = now;
    }
    state.genStats.tokenCount = currentGenTokens;
  }

  const totalTime = (now - state.genStats.startTime) / 1000;
  
  if (state.genStats.firstTokenTime > 0) {
    const activeGenTimeSec = state.genStats.activeGenTime / 1000;
    const tps = activeGenTimeSec > 0 ? state.genStats.tokenCount / activeGenTimeSec : 0;
    statusTokensSec.textContent = `${tps.toFixed(1)} t/s`;
    
    const ttft = (state.genStats.firstTokenTime - state.genStats.startTime) / 1000;
    if (isFinal) {
      statusGenInfo.textContent = `${state.genStats.tokenCount} tokens in ${totalTime.toFixed(1)}s (TPS: ${tps.toFixed(1)}, TTFT: ${ttft.toFixed(2)}s)`;
    } else {
      statusGenInfo.textContent = `Generating... ${state.genStats.tokenCount} tokens (${totalTime.toFixed(1)}s)`;
    }
  } else {
    statusGenInfo.textContent = `Waiting for LLM... (${totalTime.toFixed(1)}s)`;
  }
}

function getActivityPreview(msg) {
  if (!msg) return 'Streaming...';

  // Tool calls: show tool name + tail of arguments being generated
  if (msg.function_call) {
    const fc = msg.function_call;
    const name = fc.name || 'tool';
    const args = typeof fc.arguments === 'string' ? fc.arguments : JSON.stringify(fc.arguments || '');
    if (args.length > 0) {
      return `🛠️ ${name}: ${getLastWords(args.slice(-300), 20)}`;
    }
    return `🛠️ Calling ${name}...`;
  }

  // Tool results (FUNCTION role): show which tool completed + brief result preview
  if (msg.role === 'function') {
    const name = msg.name || 'tool';
    const content = (msg.content || '').trim();
    if (content.length > 0) {
      return `✅ ${name}: ${getLastWords(content.slice(-300), 15)}`;
    }
    return `✅ ${name} completed`;
  }

  // Regular content or reasoning
  const text = ((msg.reasoning_content || '') + (msg.content || '')).slice(-300);
  return getLastWords(text, 20) || 'Streaming...';
}

function getLastWords(text, count) {
  if (!text) return '';
  // Remove markdown syntax for cleaner activity display
  const clean = text.replace(/[#*`_\[\]()]/g, ' ').replace(/\s+/g, ' ').trim();
  const words = clean.split(' ');
  if (words.length <= count) return clean;
  return '... ' + words.slice(-count).join(' ');
}

// ── Utilities ────────────────────────────────────────────────────────────────

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatMultimodalContent(text) {
  if (typeof text !== 'string') return text;
  const visionEnabled = settingVisionEnabled ? settingVisionEnabled.checked : false;
  if (visionEnabled) return text;

  // Strip image data, leave placeholder if vision is disabled
  const imageRegex = /!\[(.*?)\]\((data:image\/[^;]+;base64,[a-zA-Z0-9+/=]+)\)/g;
  return text.replace(imageRegex, "[Image: $1]");
}



// ── Image Handling ───────────────────────────────────────────────────────────

function insertImageMarkdown(base64Data, filename) {
  const markdown = `![${filename}](${base64Data})`;
  const startPos = chatInput.selectionStart;
  const endPos = chatInput.selectionEnd;
  const text = chatInput.value;
  chatInput.value = text.substring(0, startPos) + markdown + text.substring(endPos);
  
  // Resize first, then set selection — setting height='auto' can shift layout
  // and invalidate caret position if we set selection before resize.
  autoResize(chatInput);
  chatInput.selectionStart = chatInput.selectionEnd = startPos + markdown.length;
  chatInput.focus();
}

function processImageFile(file) {
  if (!file || !file.type.startsWith('image/')) return;

  const maxSize = settingMaxImageSize ? parseInt(settingMaxImageSize.value) : 1024;
  const reader = new FileReader();

  reader.onload = (e) => {
    const img = new Image();
    img.onload = () => {
      let width = img.width;
      let height = img.height;

      if (width > maxSize || height > maxSize) {
        if (width > height) {
          height = Math.round((height * maxSize) / width);
          width = maxSize;
        } else {
          width = Math.round((width * maxSize) / height);
          height = maxSize;
        }
      }

      const canvas = document.createElement('canvas');
      canvas.width = width;
      canvas.height = height;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0, width, height);

      const mimeType = file.type === 'image/jpeg' ? 'image/jpeg' : 'image/png';
      const dataUrl = canvas.toDataURL(mimeType, 0.9);
      insertImageMarkdown(dataUrl, file.name || 'image');
    };
    img.src = e.target.result;
  };
  reader.readAsDataURL(file);
}

function processDocFile(file) {
  if (!file) return;

  const statusText = document.getElementById('statusText');
  if (statusText) statusText.textContent = `Parsing ${file.name}...`;

  const formData = new FormData();
  formData.append('file', file);

  fetch('/api/parse', {
    method: 'POST',
    body: formData
  })
  .then(resp => {
    if (!resp.ok) throw new Error(`Parse failed: ${resp.statusText}`);
    return resp.json();
  })
  .then(data => {
    if (data.text) {
      const header = `--- DOCUMENT: ${file.name} ---`;
      const footer = `--- END DOCUMENT ---`;
      const fullText = `\n${header}\n${data.text}\n${footer}\n`;

      const start = chatInput.selectionStart;
      const end = chatInput.selectionEnd;
      const oldVal = chatInput.value;
      chatInput.value = oldVal.substring(0, start) + fullText + oldVal.substring(end);

      // Resize first, then set selection — setting height='auto' can shift layout
      autoResize(chatInput);
      chatInput.selectionStart = chatInput.selectionEnd = start + fullText.length;
      chatInput.focus();
    }
    if (statusText) statusText.textContent = '';
  })
  .catch(err => {
    console.error(err);
    if (statusText) statusText.textContent = `Error: ${err.message}`;
    showInSystemToastBar(`⚠️ Failed to parse document: ${err.message}`);
  });
}

if (insertImageBtn && imageInput) {
  insertImageBtn.addEventListener('click', () => imageInput.click());
  imageInput.addEventListener('change', (e) => {
    if (e.target.files && e.target.files.length > 0) {
      processImageFile(e.target.files[0]);
    }
    e.target.value = ''; // Reset input
  });
}

if (insertDocBtn && docInput) {
  insertDocBtn.addEventListener('click', () => docInput.click());
  docInput.addEventListener('change', (e) => {
    if (e.target.files && e.target.files.length > 0) {
      processDocFile(e.target.files[0]);
    }
    e.target.value = ''; // Reset input
  });
}

chatInput.addEventListener('dragover', (e) => {
  e.preventDefault();
  chatInput.classList.add('drag-over');
});

chatInput.addEventListener('dragleave', () => {
  chatInput.classList.remove('drag-over');
});

chatInput.addEventListener('drop', (e) => {
  e.preventDefault();
  chatInput.classList.remove('drag-over');
  if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
    Array.from(e.dataTransfer.files).forEach(file => {
      if (file.path) {
        insertAtCursor(`"${file.path}" `);
      } else {
        // Fallback: search backend for the file by name to get absolute path
        fetch('/api/find_file', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: file.name })
        })
        .then(res => res.json())
        .then(data => {
          if (data.matches && data.matches.length === 1) {
            insertAtCursor(`"${data.matches[0]}" `);
          } else if (data.matches && data.matches.length > 1) {
            insertAtCursor(`"${data.matches[0]}" /* Warning: multiple matches found for ${file.name} */ `);
          } else {
            insertAtCursor(`"${file.name}" `);
          }
        })
        .catch(err => {
          console.error(err);
          insertAtCursor(`"${file.name}" `);
        });
      }
    });
  } else if (e.dataTransfer.getData('text')) {
    insertAtCursor(e.dataTransfer.getData('text'));
  }
});

function insertAtCursor(text) {
  const start = chatInput.selectionStart;
  const end = chatInput.selectionEnd;
  const oldVal = chatInput.value;
  chatInput.value = oldVal.substring(0, start) + text + oldVal.substring(end);

  // Resize first, then set selection — setting height='auto' can shift layout
  autoResize(chatInput);
  chatInput.selectionStart = chatInput.selectionEnd = start + text.length;
  chatInput.focus();
}

chatInput.addEventListener('paste', (e) => {
  if (e.clipboardData && e.clipboardData.items) {
    const items = e.clipboardData.items;
    for (let i = 0; i < items.length; i++) {
      if (items[i].type.indexOf('image') !== -1) {
        e.preventDefault();
        const file = items[i].getAsFile();
        processImageFile(file);
      }
    }
  }
});

// ── Event listeners ──────────────────────────────────────────────────────────

// ── Image Preview System ─────────────────────────────────────────────────────
// Parse textarea content for ![name](data:image/...base64) patterns and show thumbnails.
const imagePreviewContainer = document.getElementById('imagePreviewContainer');

// Regex to match markdown image syntax with base64 data URIs
const IMAGE_MARKDOWN_RE = /!\[([^\]]*)\]\((data:image\/[^;]+;base64,[a-zA-Z0-9+/=]+\))/g;

// Debounce timer for image preview updates (avoids lag with large base64 strings)
let previewUpdateTimer = null;

/** Update the image preview thumbnails based on current textarea content. */
function updateImagePreviews() {
  if (!imagePreviewContainer) return;
  const text = chatInput.value;
  const matches = [...text.matchAll(IMAGE_MARKDOWN_RE)];

  // Clear existing previews
  imagePreviewContainer.innerHTML = '';

  for (const match of matches) {
    const [, name, dataUri] = match;

    const wrapper = document.createElement('div');
    wrapper.className = 'image-preview-thumbnail';
    wrapper.title = name || 'Image attachment';

    const img = document.createElement('img');
    img.src = dataUri;
    img.alt = name || 'preview';
    img.loading = 'eager'; // Show immediately, these are already in memory

    // Remove button — strips the corresponding markdown from textarea
    const removeBtn = document.createElement('button');
    removeBtn.className = 'image-preview-remove';
    removeBtn.textContent = '×';
    removeBtn.title = 'Remove image';
    const imageMarkdown = match[0]; // Capture before closure
    removeBtn.addEventListener('click', () => {
      chatInput.value = chatInput.value.replace(imageMarkdown, '').replace(/\n{2,}/g, '\n').trim();
      autoResize(chatInput);
      updateImagePreviews();
    });

    wrapper.appendChild(img);
    wrapper.appendChild(removeBtn);
    imagePreviewContainer.appendChild(wrapper);
  }
}

// Auto-resize on every input event. The autoResize function preserves cursor position internally.
chatInput.addEventListener('input', () => {
  autoResize(chatInput);
  clearTimeout(previewUpdateTimer);
  previewUpdateTimer = setTimeout(updateImagePreviews, 200);
});
continueBtn.onclick = continueMessage;

chatInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  } else if (e.key === 'Enter' && e.shiftKey && e.ctrlKey) {
    e.preventDefault();
    continueMessage();
  } else if (e.key === 'R' && e.shiftKey && e.ctrlKey) {
    e.preventDefault();
    onRetryClick();
  }
});

// ── Keep focus in chatInput when clicking anywhere in the input area ──────────
// Clicking buttons, toggles, or empty space should not steal focus from #chatInput.
// We defer focus to the task queue (setTimeout 0) so it runs AFTER all synchronous
// click handlers have finished, including any that call chatInput.focus() themselves
// (e.g., insertImageMarkdown, insertAtCursor). This preserves button functionality
// while ensuring focus returns to the textarea for immediate typing.
const inputArea = document.querySelector('.input-area');
if (inputArea) {
  inputArea.addEventListener('click', (e) => {
    // Don't steal focus from checkbox toggles (AFK, Auto-Ask) — let them toggle normally
    if (e.target.type === 'checkbox' || e.target.closest('label:has(input[type="checkbox"])')) return;
    if (document.activeElement !== chatInput) {
      setTimeout(() => chatInput.focus(), 0);
    }
  });
}

sendBtn.addEventListener('click', sendMessage);
if (stopBtn) stopBtn.addEventListener('click', () => send({ type: 'stop' }));

// Generic pause/resume toggle button factory — works for any agent instance
function createPauseButton(btn, instanceSource) {
  // instanceSource: a function returning the instance name (e.g. () => sessionNameInput.value)
  if (!btn || typeof instanceSource !== 'function') return;
  
  btn.addEventListener('click', async () => {
    const sessionName = instanceSource();
    if (btn.textContent.includes('Pause')) {
      // Send WebSocket 'pause' message to halt ALL instances (orchestrator + all sub-agents)
      send({ type: 'pause' });
      btn.textContent = '▶️ Resume';
      // Update local state for all ACTIVE agents only
      Object.keys(state.subAgents).forEach(name => {
        const agent = state.subAgents[name];
        if (agent && agent.active) {
          agent.is_halted = true;
        }
      });
    } else {
      // Send WebSocket 'resume_all' message to resume ALL halted instances
      send({ type: 'resume_all' });
      btn.textContent = '⏸ Pause';
      // Update local state for all ACTIVE agents only
      Object.keys(state.subAgents).forEach(name => {
        const agent = state.subAgents[name];
        if (agent && agent.active) {
          agent.is_halted = false;
        }
      });
    }
  });
}

// Main chat pause button
if (pauseBtn) createPauseButton(pauseBtn, () => getActiveInstanceName());

// Main chat terminate button
const terminateBtn = $('#terminateBtn');
if (terminateBtn) {
  terminateBtn.addEventListener('click', () => {
    const activeInstance = getActiveInstanceName();
    if (!activeInstance) return;
    // Never allow terminating the root orchestrator — it breaks the session.
    if (isSessionPrimaryAgent(activeInstance)) {
      alert(`Cannot terminate the root agent '${activeInstance}'. Use Stop instead.`);
      return;
    }
    if (confirm(`Terminate ${activeInstance}?`)) {
      send({ type: 'terminate_agent_instance', instance_name: activeInstance });
    }
  });
}

const onRetryClick = () => {
  // Invalidate ALL panel caches to force full re-render (not just root)
  mainTabPanels.querySelectorAll('.messages').forEach(p => {
    p.dataset.contentKey = '';
    p.dataset.lastRenderedCount = '999999999';
  });
  retryGeneration();
};
const refreshBtn = document.getElementById('refreshBtn');
const mainRetryBtn = document.getElementById('mainRetryBtn');
if (refreshBtn) {
  refreshBtn.addEventListener('click', () => {
    send({ type: 'refresh_souls' });
    const span = refreshBtn.querySelector('span');
    const originalText = span ? span.textContent : 'Refresh Soul';
    if (span) span.textContent = 'Refreshing...';
    refreshBtn.disabled = true;
    setTimeout(() => {
      if (span) span.textContent = originalText;
      refreshBtn.disabled = false;
    }, 1500);
  });
}
if (mainRetryBtn) mainRetryBtn.addEventListener('click', onRetryClick);
resetBtn.addEventListener('click', () => {
  if (confirm('Reset the entire conversation and start a new session?')) {
    // Clear closed tabs cache so all tabs reappear after reset
    state.closedTabs.clear();
    localStorage.removeItem('agent-cascade-closed-tabs');
    // Invalidate all panel caches to force full re-render after reset
      mainTabPanels.querySelectorAll('.messages').forEach(p => {
        p.dataset.contentKey = '';
        p.dataset.lastRenderedCount = '999999999';
      });
      send({ type: 'reset' });
  }
});

agentSelect.addEventListener('change', () => {
  state.agentIndex = parseInt(agentSelect.value);
  state.viewingAgentIndex = state.agentIndex;
  renderAgentSelect(); // This will also call renderToolsForSelectedAgent
  send({ type: 'select_agent', index: state.agentIndex });
});

sessionNameInput.addEventListener('change', () => {
  state.sessionName = sessionNameInput.value.trim() || DEFAULT_SESSION_NAME;
  state.closedTabs.clear();
  localStorage.removeItem('agent-cascade-closed-tabs');
  localStorage.setItem('agent-cascade-session-name', state.sessionName);
  send({ type: 'set_session_name', name: state.sessionName });
});

function getGenerateCfg() {
  const cfg = {};
  if ($('#setting-endpoint') && $('#setting-endpoint').value.trim()) cfg.api_base = $('#setting-endpoint').value.trim();
  if ($('#setting-api-key') && $('#setting-api-key').value.trim()) cfg.api_key = $('#setting-api-key').value.trim();
  if ($('#setting-model') && $('#setting-model').value.trim()) cfg.model = $('#setting-model').value.trim();

  if ($('#setting-temperature')) cfg.temperature = parseFloat($('#setting-temperature').value);
  if ($('#setting-top-p')) cfg.top_p = parseFloat($('#setting-top-p').value);
  if ($('#setting-top-k')) cfg.top_k = parseInt($('#setting-top-k').value);
  if ($('#setting-min-p')) cfg.min_p = parseFloat($('#setting-min-p').value);
  if ($('#setting-repeat-penalty')) cfg.repeat_penalty = parseFloat($('#setting-repeat-penalty').value);
  if ($('#setting-presence-penalty')) cfg.presence_penalty = parseFloat($('#setting-presence-penalty').value);
  if ($('#setting-frequency-penalty')) cfg.frequency_penalty = parseFloat($('#setting-frequency-penalty').value);
  if ($('#setting-max-tokens')) cfg.max_tokens = parseInt($('#setting-max-tokens').value) || 2048;
  if ($('#setting-max-context')) cfg.max_input_tokens = parseInt($('#setting-max-context').value) || 32768;

  if ($('#setting-max-turns')) cfg.max_turns = parseInt($('#setting-max-turns').value) || 50;
  if ($('#setting-max-parallel')) cfg.max_parallel_agents = parseInt($('#setting-max-parallel').value) || 3;
  if ($('#setting-auto-continue')) cfg.auto_continue = $('#setting-auto-continue').checked;
  if ($('#setting-auto-rollback')) cfg.auto_rollback_on_loop = $('#setting-auto-rollback').checked;
  if ($('#setting-inner-loop-detect')) cfg.inner_loop_detect_enabled = $('#setting-inner-loop-detect').checked;
  if ($('#setting-log-api-post')) cfg.log_api_post = $('#setting-log-api-post').checked;
  if ($('#setting-max-rollbacks')) cfg.max_auto_rollbacks = parseInt($('#setting-max-rollbacks').value);
  if ($('#setting-idle-timeout')) cfg.idle_timeout_seconds = parseFloat($('#setting-idle-timeout').value);
  if ($('#setting-tool-result-max-chars')) cfg.tool_result_max_chars = parseInt($('#setting-tool-result-max-chars').value) || 10000;
  if ($('#setting-grep-char-limit')) cfg.grep_char_limit = parseInt($('#setting-grep-char-limit').value) || -1;
  if ($('#setting-grep-spillover')) cfg.grep_spillover = $('#setting-grep-spillover').checked;
  if ($('#setting-shell-char-limit')) cfg.shell_char_limit = parseInt($('#setting-shell-char-limit').value);
  if ($('#setting-code-char-limit')) cfg.code_char_limit = parseInt($('#setting-code-char-limit').value);

  // Approval timeout settings
  if (approvalTimeoutSeconds && approvalTimeoutSeconds.value) cfg.approval_timeout_seconds = parseInt(approvalTimeoutSeconds.value) || 300;
  if (approvalTimeoutEnabled.length) cfg.enable_approval_timeout = approvalTimeoutEnabled.checked;

  if ($('#setting-mcp-enabled') && !$('#setting-mcp-enabled').checked) {
    // MCP is disabled
  } else if ($('#setting-mcp-servers') && $('#setting-mcp-servers').value.trim()) {
    try {
      cfg.mcpServers = JSON.parse($('#setting-mcp-servers').value.trim());
    } catch (e) {
      console.warn('Invalid MCP Servers JSON:', e);
    }
  }

  if (workAccessFoldersRO) {
    cfg.work_access_folders_ro = workAccessFoldersRO.value.trim() ? workAccessFoldersRO.value.trim().split('\n').map(s => s.trim()).filter(s => s) : [];
  }
  if (workAccessFoldersRW) {
    cfg.work_access_folders_rw = workAccessFoldersRW.value.trim() ? workAccessFoldersRW.value.trim().split('\n').map(s => s.trim()).filter(s => s) : [];
  }

  if (typeof agentDisabledTools !== 'undefined') {
    cfg.disabled_tools = agentDisabledTools;
  }

  if (defaultWorkspace && defaultWorkspace.textContent && defaultWorkspace.textContent !== 'Loading...') {
    cfg.default_workspace = defaultWorkspace.textContent.replace(' (Pending restart)', '').trim();
  }

  return cfg;
}

function sendMessage(inputEl) {
  const targetInput = inputEl instanceof HTMLElement ? inputEl : chatInput;
  const rawText = targetInput.value.trim();
  if (!rawText) return;

  const text = formatMultimodalContent(rawText);
  targetInput.value = '';
  autoResize(targetInput);
  imagePreviewContainer.innerHTML = ''; // Clear image previews after sending

  if (state.generating) {
    // Async injection: route to the active agent (session primary or selected sub-tab)
    const targetAgent = getActiveAgentName();
    send({ type: 'message', text, target_agent: targetAgent });
    return;
  }

  resetGenStats();
  const targetAgent = getActiveAgentName();
  send({
    type: 'message',
    text,
    target_agent: targetAgent,
    agent_index: state.agentIndex,
    session_name: state.sessionName,
    generate_cfg: getGenerateCfg()
  });
}

function continueMessage() {
  if (state.generating) return;

  // FIX: Send a 'continue' type instead of a regular 'message'.
  // This tells the server to resume generation without inserting a new user message.
  resetGenStats();
  const targetAgent = getActiveAgentName();
  send({
    type: 'continue',
    target_agent: targetAgent,
    agent_index: state.agentIndex,
    session_name: state.sessionName,
    generate_cfg: getGenerateCfg()
  });
}

function retryGeneration() {
  if (state.generating) return;
  resetGenStats();
  const targetAgent = getActiveAgentName();
  send({
    type: 'retry',
    target_agent: targetAgent,
    agent_index: state.agentIndex,
    session_name: state.sessionName,
    generate_cfg: getGenerateCfg()
  });
}

// ── Init ─────────────────────────────────────────────────────────────────────
ActivityBar.init();
connect();
if ($('#apply-mcp-btn')) {
  $('#apply-mcp-btn').addEventListener('click', () => {
    saveSettings();
    send({
      type: 'update_config',
      generate_cfg: getGenerateCfg()
    });
    $('#apply-mcp-btn').textContent = 'Applying...';
    setTimeout(() => {
      $('#apply-mcp-btn').textContent = 'Apply MCP Config';
    }, 2000);
  });
}

if ($('#restart-server-btn')) {
  $('#restart-server-btn').addEventListener('click', () => {
    if (confirm('Are you sure you want to restart the server? This will interrupt any active generations.')) {
      send({ type: 'restart_server' });
      const btn = $('#restart-server-btn');
      btn.textContent = 'Restarting...';
      btn.disabled = true;
      setTimeout(() => {
        window.location.reload();
      }, 3000);
    }
  });
}



// ── Telemetry Panel ──────────────────────────────────────────────────────────

function formatNumber(n) {
  if (n === undefined || n === null) return '—';
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return String(n);
}

function formatMs(ms) {
  if (!ms || ms === 0) return '—';
  if (ms >= 60000) return (ms / 60000).toFixed(1) + 'min';
  if (ms >= 1000) return (ms / 1000).toFixed(1) + 's';
  return Math.round(ms) + 'ms';
}

function getSuccessClass(rate) {
  if (rate >= 95) return 'telem-success';
  if (rate >= 75) return 'telem-warning';
  return 'telem-danger';
}

function updateTelemetryPanel(telemetry) {
  if (!telemetry) return;

  // Session stats cards
  const set = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  };

  set('telem-turns', formatNumber(telemetry.total_turns));
  set('telem-llm-calls', formatNumber(telemetry.total_llm_calls));
  set('telem-tool-calls', formatNumber(telemetry.total_tool_calls));
  set('telem-sa-calls', formatNumber(telemetry.agent_instance_calls));
  set('telem-input-tokens', formatNumber(telemetry.total_input_tokens_est));
  set('telem-output-tokens', formatNumber(telemetry.total_output_tokens_est));
  set('telem-total-tokens', formatNumber(telemetry.total_tokens));
  set('telem-avg-tps', telemetry.avg_tps ? telemetry.avg_tps.toFixed(1) : '—');
  set('telem-avg-llm-lat', formatMs(telemetry.avg_llm_latency_ms));
  set('telem-avg-tool-lat', formatMs(telemetry.avg_tool_latency_ms));
  set('telem-loops', formatNumber(telemetry.total_loops_detected));
  set('telem-compressions', formatNumber(telemetry.total_compressions));

  // Tool effectiveness table
  const toolTbody = document.getElementById('telem-tool-tbody');
  if (toolTbody && telemetry.tool_effectiveness) {
    const tools = telemetry.tool_effectiveness;
    const entries = Object.entries(tools);
    if (entries.length === 0) {
      toolTbody.innerHTML = '<tr><td colspan="4" style="text-align:center; color:var(--text-secondary)">No tool data yet</td></tr>';
    } else {
      // Sort by total calls descending
      entries.sort((a, b) => b[1].total - a[1].total);
      toolTbody.innerHTML = entries.map(([name, data]) => {
        const rateClass = getSuccessClass(data.success_rate);
        const safeName = escapeHtml(name);
        return `<tr>
          <td title="${safeName}">${safeName}</td>
          <td>${data.total}</td>
          <td class="${rateClass}">${data.success_rate}%</td>
          <td>${formatMs(data.avg_latency_ms)}</td>
        </tr>`;
      }).join('');
    }
  }
}

function updateTelemetryConfigTable(configs) {
  const configTbody = document.getElementById('telem-config-tbody');
  if (!configTbody) return;

  if (!configs || configs.length === 0) {
    configTbody.innerHTML = '<tr><td colspan="4" style="text-align:center; color:var(--text-secondary)">No config data yet</td></tr>';
    return;
  }

  configTbody.innerHTML = configs.map(c => {
    const desc = c.config_description || {};
    const safeModel = desc.model ? escapeHtml(desc.model) : '';
    const label = desc.model
      ? `${safeModel} T=${desc.temperature ?? '?'}`
      : c.config_fingerprint.slice(0, 8);
    return `<tr>
      <td title="${escapeHtml(c.config_fingerprint)}"><span class="telem-config-tag">${label}</span></td>
      <td>${c.turns}</td>
      <td>${formatNumber(c.total_tokens)}</td>
      <td>${formatMs(c.avg_turn_duration_ms)}</td>
    </tr>`;
  }).join('');
}

// Fetch full telemetry from API (for config comparison data not in WebSocket)
async function fetchTelemetry() {
  try {
    const res = await fetch('/api/telemetry');
    const data = await res.json();
    if (data.session) updateTelemetryPanel(data.session);
    if (data.configs) updateTelemetryConfigTable(data.configs);
  } catch (err) {
    console.warn('Failed to fetch telemetry:', err);
  }
}

// Export telemetry JSONL
const telemExportBtn = document.getElementById('telem-export-btn');
if (telemExportBtn) {
  telemExportBtn.addEventListener('click', () => {
    window.open('/api/telemetry/export', '_blank');
  });
}

// Refresh telemetry when the Telemetry settings tab becomes active
document.querySelectorAll('.settings-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    if (tab.dataset.tab === 'settings-telemetry') {
      fetchTelemetry();
    }
  });
});

// ── API Router Management ──────────────────────────────────────────────────

let apiEndpointsListenersAttached = false; // Guard against accumulating listeners on listEl

function renderApiEndpoints() {
  const listEl = document.getElementById('api-endpoints-list');
  if (!listEl) return;

  const endpoints = (state.api_router && state.api_router.endpoints) ? state.api_router.endpoints : [];
  
  if (endpoints.length === 0) {
    listEl.innerHTML = `
      <div class="api-endpoint-empty" style="text-align:center;color:var(--text-muted);padding:12px;font-size:12px;">
        No extra endpoints configured. Agents use the General Settings API.
      </div>`;
    return;
  }

  // Preserve open states
  const openDetails = new Set();
  listEl.querySelectorAll('.api-endpoint-details.open').forEach(el => {
    openDetails.add(el.dataset.id);
  });

  listEl.innerHTML = endpoints.map((ep, index) => {
      const isFirst = index === 0;
      const isLast = index === endpoints.length - 1;
      const isOpen = openDetails.has(ep.id);

      // Defaults for new fields (backward compat: old endpoints may not have these)
      const epVision = ep.vision_enabled !== false;       // default True
      const epCustomSampling = !!ep.use_custom_sampling;  // default False

      return `
         <div class="api-endpoint-card ${ep.enabled ? '' : 'disabled'}" data-id="${escapeHtml(ep.id)}">
           <div class="api-endpoint-header">
             <input type="checkbox" class="api-endpoint-toggle" ${ep.enabled ? 'checked' : ''} title="Enable/Disable">
             <div class="api-endpoint-name" title="${escapeHtml(ep.name)}">${escapeHtml(ep.name)}</div>
             <div class="api-endpoint-meta">${escapeHtml(ep.model || 'No model specified')}</div>

             <div class="api-endpoint-arrows">
               <button class="api-endpoint-move-up" ${isFirst ? 'disabled style="opacity:0.2"' : ''} title="Move Up">▲</button>
               <button class="api-endpoint-move-down" ${isLast ? 'disabled style="opacity:0.2"' : ''} title="Move Down">▼</button>
             </div>
             
             <button class="api-endpoint-expand ${isOpen ? 'open' : ''}">▸</button>
           </div>

           <div class="api-endpoint-details ${isOpen ? 'open' : ''}" data-id="${escapeHtml(ep.id)}">
             <label class="setting-field">
               <span>Name</span>
               <input type="text" class="ep-input-name" value="${escapeHtml(ep.name)}">
             </label>
             <label class="setting-field">
               <span>API Endpoint</span>
               <input type="text" class="ep-input-base" value="${escapeHtml(ep.api_base)}">
             </label>
             <label class="setting-field">
               <span>API Key</span>
               <div class="api-key-field">
                 <input type="password" class="ep-input-key" value="${escapeHtml(ep.api_key)}">
                 <button class="api-key-toggle" title="Show/Hide">👁</button>
               </div>
             </label>
             <label class="setting-field">
               <span>Model</span>
               <input type="text" class="ep-input-model" value="${escapeHtml(ep.model)}">
             </label>
             <div style="display:flex;gap:8px;">
               <label class="setting-field" style="flex:1;">
                 <span>Retries</span>
                 <input type="number" min="0" max="10" class="ep-input-retries" value="${ep.max_retries}">
               </label>
               <label class="setting-field" style="flex:1;">
                 <span>Concurrency</span>
                 <input type="number" min="0" max="100" class="ep-input-concurrency" value="${ep.concurrency_limit || 0}" title="0 = Unlimited. Set to 1 for local servers like LM Studio.">
               </label>
               <label class="setting-field" style="flex:1;">
                 <span>Token Limit</span>
                 <input type="number" min="0" step="1000" class="ep-input-tokens" value="${ep.max_input_tokens || 0}" title="0 = Use General Settings. Caps context for this endpoint.">
               </label>
             </div>
             <div style="display:flex;gap:8px;margin-top:6px;">
               <label class="setting-field" style="flex:1;">
                 <span>Backoff Base (s)</span>
                 <input type="number" min="0.1" max="60" step="0.5" class="ep-input-base-delay" value="${ep.base_retry_delay || 1.0}" title="Base delay for exponential backoff on retry">
               </label>
               <label class="setting-field" style="flex:1;">
                 <span>Backoff Max (s)</span>
                 <input type="number" min="1" max="60" step="1" class="ep-input-max-delay" value="${ep.max_retry_delay || 30.0}" title="Maximum cap on retry delay">
               </label>
               <label class="setting-field" style="flex:1;">
                 <span>Rate Limit (rpm)</span>
                 <input type="number" min="0" step="1" class="ep-input-rate-limit" value="${ep.rate_limit_rpm || 0}" title="Requests per minute. 0 = unlimited">
               </label>
             </div>
             
             <!-- Per-endpoint feature toggles -->
             <label class="setting-field toggle-field" style="margin:8px 0 0 0;font-size:12px;cursor:pointer;">
               <span>👁 Vision Enabled</span>
               <input type="checkbox" class="ep-input-vision" ${epVision ? 'checked' : ''}>
             </label>
             <label class="setting-field toggle-field" style="margin:4px 0 0 0;font-size:12px;cursor:pointer;" title="When enabled, per-endpoint sampler params override global settings">
               <span>🎲 Custom Sampling</span>
               <input type="checkbox" class="ep-input-custom-sampling" ${epCustomSampling ? 'checked' : ''}>
             </label>
             
             <!-- Collapsible Sampling Parameters section (shown/hidden based on custom sampling toggle) -->
             <div class="ep-sampling-section ${epCustomSampling ? '' : 'ep-sampling-hidden'}">
               <div class="ep-sampling-header">Sampling Parameters</div>
               <div style="display:flex;gap:8px;margin-top:4px;">
                 <label class="setting-field" style="flex:1;">
                   <span>Temperature <output>${ep.temperature || '—'}</output></span>
                   <input type="range" min="0" max="2" step="0.05" class="ep-input-temperature" value="${ep.temperature || 0}">
                 </label>
                 <label class="setting-field" style="flex:1;">
                   <span>Top-P <output>${ep.top_p || '—'}</output></span>
                   <input type="range" min="0" max="1" step="0.01" class="ep-input-top-p" value="${ep.top_p || 0}">
                 </label>
               </div>
               <div style="display:flex;gap:8px;margin-top:4px;">
                 <label class="setting-field" style="flex:1;">
                   <span>Top-K</span>
                   <input type="number" min="0" max="200" step="1" class="ep-input-top-k" value="${ep.top_k || 0}" title="0 = use default">
                 </label>
                 <label class="setting-field" style="flex:1;">
                   <span>Min-P</span>
                   <input type="number" min="0" max="1" step="0.01" class="ep-input-min-p" value="${ep.min_p || 0}" title="0 = use default">
                 </label>
                 <label class="setting-field" style="flex:1;">
                   <span>Repeat Penalty</span>
                   <input type="number" min="1" max="2" step="0.01" class="ep-input-repeat-penalty" value="${ep.repeat_penalty || 0}" title="0 = use default">
                 </label>
               </div>
               <div style="display:flex;gap:8px;margin-top:4px;">
                 <label class="setting-field" style="flex:1;">
                   <span>Presence Penalty</span>
                   <input type="number" min="-2" max="2" step="0.1" class="ep-input-presence-penalty" value="${ep.presence_penalty || 0}" title="0 = use default">
                 </label>
                 <label class="setting-field" style="flex:1;">
                   <span>Frequency Penalty</span>
                   <input type="number" min="-2" max="2" step="0.1" class="ep-input-frequency-penalty" value="${ep.frequency_penalty || 0}" title="0 = use default">
                 </label>
                 <label class="setting-field" style="flex:1;">
                   <span>Max Tokens</span>
                   <input type="number" min="0" step="256" class="ep-input-max-tokens" value="${ep.max_tokens || 0}" title="0 = use default">
                 </label>
               </div>
             </div>

             <button class="api-endpoint-delete">Delete Endpoint</button>
           </div>
         </div>
       `;
    }).join('');

    // ── Event Delegation (no per-card listeners to leak) ──────────────────────
  // Flag guard prevents accumulating listeners on listEl across render calls
  if (!apiEndpointsListenersAttached) {
    apiEndpointsListenersAttached = true;

    // Click handler for buttons (expand, toggle visibility, move up/down, delete)
    listEl.addEventListener('click', handleApiEndpointClick);

    // Change handler for toggle checkbox (enable/disable endpoint)
    listEl.addEventListener('change', handleApiEndpointToggle);

    // Blur handler for input fields — save edits when focus leaves an input (capture phase)
    listEl.addEventListener('blur', handleApiEndpointBlur, true);

    // Keydown handler for input fields — Enter triggers save via blur (capture phase)
    listEl.addEventListener('keydown', handleApiEndpointKeydown, true);

    // Live update of range slider output values (temperature, top-p display)
    listEl.addEventListener('input', handleApiEndpointRangeInput);
  }
}

// ── Delegated event handlers for API endpoint cards ────────────────────────

function handleApiEndpointClick(e) {
  const card = e.target.closest('.api-endpoint-card');
  if (!card) return;
  const id = card.dataset.id;
  const endpoints = state.api_router?.endpoints || [];
  const ep = endpoints.find(ep => ep.id === id);
  if (!ep) return;

  if (e.target.closest('.api-endpoint-expand')) {
    // Expand/Collapse details panel
    const btn = e.target.closest('.api-endpoint-expand');
    const details = card.querySelector('.api-endpoint-details');
    btn.classList.toggle('open');
    details.classList.toggle('open');
  } else if (e.target.closest('.api-key-toggle')) {
    // Show/Hide API key
    const input = card.querySelector('.ep-input-key');
    if (input.type === 'password') {
      input.type = 'text';
      e.target.closest('.api-key-toggle').style.color = 'var(--accent)';
    } else {
      input.type = 'password';
      e.target.closest('.api-key-toggle').style.color = '';
    }
  } else if (e.target.closest('.api-endpoint-move-up')) {
    // Move endpoint up one position
    const idx = endpoints.indexOf(ep);
    if (idx > 0) {
      [endpoints[idx-1], endpoints[idx]] = [endpoints[idx], endpoints[idx-1]];
      sendApiRouterUpdate();
    }
  } else if (e.target.closest('.api-endpoint-move-down')) {
    // Move endpoint down one position
    const idx = endpoints.indexOf(ep);
    if (idx < endpoints.length - 1) {
      [endpoints[idx+1], endpoints[idx]] = [endpoints[idx], endpoints[idx+1]];
      sendApiRouterUpdate();
    }
  } else if (e.target.closest('.api-endpoint-delete')) {
    // Delete endpoint and clean up assignments
    if (confirm(`Delete endpoint "${ep.name}"?`)) {
      state.api_router.endpoints = endpoints.filter(e => e.id !== id);
      if (state.api_router.agent_priorities) {
        for (const type in state.api_router.agent_priorities) {
          state.api_router.agent_priorities[type] = state.api_router.agent_priorities[type].filter(e => e !== id);
        }
      }
      sendApiRouterUpdate();
    }
  }
}

function handleApiEndpointToggle(e) {
  // Enable/disable endpoint toggle
  const toggle = e.target.closest('.api-endpoint-toggle');
  if (toggle) {
    const card = toggle.closest('.api-endpoint-card');
    const endpoints = state.api_router?.endpoints || [];
    const ep = endpoints.find(ep => ep.id === card.dataset.id);
    if (ep) { ep.enabled = e.target.checked; sendApiRouterUpdate(); }
    return;
  }

  // Custom sampling toggle — show/hide the sampling section and save state
  const customSamplingToggle = e.target.closest('.ep-input-custom-sampling');
  if (customSamplingToggle) {
    const card = customSamplingToggle.closest('.api-endpoint-card');
    const endpoints = state.api_router?.endpoints || [];
    const ep = endpoints.find(ep => ep.id === card.dataset.id);
    if (ep) {
      ep.use_custom_sampling = e.target.checked;
      // Toggle visibility of the sampling parameters section
      const section = card.querySelector('.ep-sampling-section');
      if (section) {
        section.classList.toggle('ep-sampling-hidden', !e.target.checked);
      }
      sendApiRouterUpdate();
    }
    return;
  }

  // Vision toggle — just save state immediately
  const visionToggle = e.target.closest('.ep-input-vision');
  if (visionToggle) {
    const card = visionToggle.closest('.api-endpoint-card');
    const endpoints = state.api_router?.endpoints || [];
    const ep = endpoints.find(ep => ep.id === card.dataset.id);
    if (ep) {
      ep.vision_enabled = e.target.checked;
      sendApiRouterUpdate();
    }
  }
}

// Helper: safely read a numeric value from an input element within a card.
// Returns defaultValue if the element is missing, parsing fails, or result is NaN/null/undefined.
function _epVal(card, selector, parseFn, defaultValue) {
  const el = card.querySelector(selector);
  if (!el) return defaultValue;
  try {
    const v = parseFn(el.value);
    return (Number.isNaN(v) || v === null || v === undefined) ? defaultValue : v;
  } catch { return defaultValue; }
}

function handleApiEndpointBlur(e) {
  const card = e.target.closest('.api-endpoint-card');
  if (!card || !card.querySelector(':scope > .api-endpoint-header')) return;
  const id = card.dataset.id;
  const endpoints = state.api_router?.endpoints || [];
  const ep = endpoints.find(ep => ep.id === id);
  if (!ep) return;

  // String fields (trim, fallback to defaults where applicable)
  ep.name = _epVal(card, '.ep-input-name', v => v.trim(), '');
  ep.api_base = _epVal(card, '.ep-input-base', v => v.trim(), '');
  ep.api_key = _epVal(card, '.ep-input-key', v => v.trim() || 'EMPTY', 'EMPTY');
  ep.model = _epVal(card, '.ep-input-model', v => v.trim(), '');

  // Integer fields (NaN-safe with zero defaults)
  ep.max_retries = _epVal(card, '.ep-input-retries', parseInt, 0);
  ep.concurrency_limit = _epVal(card, '.ep-input-concurrency', parseInt, 0);
  ep.max_input_tokens = _epVal(card, '.ep-input-tokens', parseInt, 0);

  // Float fields (NaN-safe with appropriate defaults)
  ep.base_retry_delay = _epVal(card, '.ep-input-base-delay', parseFloat, 1.0);
  ep.max_retry_delay = _epVal(card, '.ep-input-max-delay', parseFloat, 30.0);
  ep.rate_limit_rpm = _epVal(card, '.ep-input-rate-limit', parseInt, 0);

  // Checkbox toggles (optional elements)
  const visionCb = card.querySelector('.ep-input-vision');
  if (visionCb) ep.vision_enabled = visionCb.checked;
  const customSamplingCb = card.querySelector('.ep-input-custom-sampling');
  if (customSamplingCb) ep.use_custom_sampling = customSamplingCb.checked;

  // Sampler parameters (NaN-safe, zero means "use default")
  ep.temperature = _epVal(card, '.ep-input-temperature', parseFloat, 0);
  ep.top_p = _epVal(card, '.ep-input-top-p', parseFloat, 0);
  ep.top_k = _epVal(card, '.ep-input-top-k', parseInt, 0);
  ep.min_p = _epVal(card, '.ep-input-min-p', parseFloat, 0);
  ep.repeat_penalty = _epVal(card, '.ep-input-repeat-penalty', parseFloat, 0);
  ep.presence_penalty = _epVal(card, '.ep-input-presence-penalty', parseFloat, 0);
  ep.frequency_penalty = _epVal(card, '.ep-input-frequency-penalty', parseFloat, 0);
  ep.max_tokens = _epVal(card, '.ep-input-max-tokens', parseInt, 0);

  sendApiRouterUpdate();
}

function handleApiEndpointKeydown(e) {
  if (e.key !== 'Enter') return;
  const input = e.target.closest('input.ep-input-name, input.ep-input-base, input.ep-input-key, input.ep-input-model, input.ep-input-retries, input.ep-input-concurrency, input.ep-input-tokens, input.ep-input-base-delay, input.ep-input-max-delay, input.ep-input-rate-limit, input.ep-input-top-k, input.ep-input-min-p, input.ep-input-repeat-penalty, input.ep-input-presence-penalty, input.ep-input-frequency-penalty, input.ep-input-max-tokens');
  if (input) input.blur();
}

// Live update range slider output values (temperature, top-p display next to label)
function handleApiEndpointRangeInput(e) {
  const slider = e.target.closest('input.ep-input-temperature, input.ep-input-top-p');
  if (!slider) return;
  // Find the <output> element in the same setting-field label
  const outputEl = slider.parentElement.querySelector('output');
  if (outputEl && parseFloat(slider.value) > 0) {
    outputEl.textContent = slider.value;
  } else if (outputEl) {
    outputEl.textContent = '—';
  }
}

function renderAgentApiAssignments() {
  const container = document.getElementById('agent-api-assignments');
  if (!container) return;
  
  const endpoints = (state.api_router && state.api_router.endpoints) ? state.api_router.endpoints : [];
  const priorities = (state.api_router && state.api_router.agent_priorities) ? state.api_router.agent_priorities : {};
  
  // Show placeholder when agents haven't loaded yet (no endpoints to assign anyway)
  if (endpoints.length === 0) {
    container.innerHTML = `
      <div style="text-align:center;color:var(--text-muted);padding:12px;font-size:12px;">
        Add endpoints above, then assign them to agent types.
      </div>`;
    return;
  }

  // Extract unique agent types and their friendly names
  const typeToName = {};
  if (state.agents) {
    state.agents.forEach(a => {
      const type = (a.agent_type || 'orchestrator').toLowerCase();
      if (!typeToName[type]) typeToName[type] = a.name;
    });
  }
  
  const agentTypes = Object.keys(typeToName);
  // Ensure orchestrator and coder are always in the list even if missing
  if (!agentTypes.includes('orchestrator')) {
    agentTypes.unshift('orchestrator');
    typeToName['orchestrator'] = 'Orchestrator';
  }
  if (!agentTypes.includes('coder') && !agentTypes.includes('coder_agent')) {
    agentTypes.push('coder');
    typeToName['coder'] = 'Coder';
  }
  // Ensure Compressor is always in the list so users can assign API endpoints to it
  if (!agentTypes.includes('compressor')) {
    agentTypes.push('compressor');
    typeToName['compressor'] = 'Compressor';
  }
  // Ensure Security is always in the list so users can assign API endpoints to it
  if (!agentTypes.includes('security')) {
    agentTypes.push('security');
    typeToName['security'] = 'Security';
  }

  if (endpoints.length === 0) {
    container.innerHTML = `
      <div style="text-align:center;color:var(--text-muted);padding:12px;font-size:12px;">
        Add endpoints above, then assign them to agent types.
      </div>`;
    return;
  }

  container.innerHTML = agentTypes.map(type => {
    const assignedIds = priorities[type] || [];
    const availableEndpoints = endpoints.filter(ep => !assignedIds.includes(ep.id));
    const friendlyName = typeToName[type] || (type.charAt(0).toUpperCase() + type.slice(1));
    
    let addSelectHtml = '';
    if (availableEndpoints.length > 0) {
      addSelectHtml = `
        <select class="agent-api-add-select">
          <option value="">+ Add Endpoint</option>
          ${availableEndpoints.map(ep => `<option value="${escapeHtml(ep.id)}">${escapeHtml(ep.name)}</option>`).join('')}
        </select>
      `;
    }

    let listHtml = assignedIds.map((eid, idx) => {
      const ep = endpoints.find(e => e.id === eid);
      if (!ep) return '';
      return `
        <div class="agent-api-priority-item">
          <span class="priority-num">${idx + 1}.</span>
          <span class="priority-name" title="${escapeHtml(ep.name)}">${escapeHtml(ep.name)}</span>
          <button class="agent-api-move-up" data-id="${escapeHtml(eid)}" ${idx === 0 ? 'disabled style="opacity:0.2"' : ''}>▲</button>
          <button class="agent-api-move-down" data-id="${escapeHtml(eid)}" ${idx === assignedIds.length - 1 ? 'disabled style="opacity:0.2"' : ''}>▼</button>
          <button class="agent-api-remove" data-id="${escapeHtml(eid)}">✕</button>
        </div>
      `;
    }).join('');

    return `
      <div class="agent-api-assignment" data-type="${escapeHtml(type)}">
        <div class="agent-api-assignment-header">
          <div class="agent-api-assignment-name">${escapeHtml(friendlyName)}:</div>
          <div class="agent-api-assignment-add">
            ${addSelectHtml}
          </div>
        </div>
        ${listHtml ? `<div class="agent-api-assignment-list">${listHtml}</div>` : `<div class="agent-api-default-label">(using General Settings API default)</div>`}
      </div>
    `;
  }).join('');

  // Bind Events
  container.querySelectorAll('.agent-api-assignment').forEach(block => {
    const type = block.dataset.type;
    const assignedIds = priorities[type] || [];

    // Add Select
    const select = block.querySelector('.agent-api-add-select');
    if (select) {
      select.addEventListener('change', (e) => {
        const id = e.target.value;
        if (id) {
          if (!priorities[type]) priorities[type] = [];
          priorities[type].push(id);
          sendApiRouterUpdate();
        }
      });
    }

    // List actions
    block.querySelectorAll('.agent-api-priority-item').forEach(item => {
      const id = item.querySelector('.agent-api-remove').dataset.id;
      
      item.querySelector('.agent-api-remove').addEventListener('click', () => {
        priorities[type] = priorities[type].filter(eid => eid !== id);
        if (priorities[type].length === 0) delete priorities[type];
        sendApiRouterUpdate();
      });

      const upBtn = item.querySelector('.agent-api-move-up');
      if (upBtn) {
        upBtn.addEventListener('click', () => {
          const idx = priorities[type].indexOf(id);
          if (idx > 0) {
            [priorities[type][idx-1], priorities[type][idx]] = [priorities[type][idx], priorities[type][idx-1]];
            sendApiRouterUpdate();
          }
        });
      }

      const downBtn = item.querySelector('.agent-api-move-down');
      if (downBtn) {
        downBtn.addEventListener('click', () => {
          const idx = priorities[type].indexOf(id);
          if (idx < priorities[type].length - 1) {
            [priorities[type][idx+1], priorities[type][idx]] = [priorities[type][idx], priorities[type][idx+1]];
            sendApiRouterUpdate();
          }
        });
      }
    });
  });
}

function sendApiRouterUpdate() {
  if (!state.api_router) return;
  // Send the bulk update via WebSocket
  send({
    type: 'update_endpoints',
    endpoints: state.api_router.endpoints || [],
    agent_priorities: state.api_router.agent_priorities || {}
  });
  // Optimistically re-render locally
  renderApiEndpoints();
  renderAgentApiAssignments();
}

const btnAddEndpoint = document.getElementById('btn-add-endpoint');
if (btnAddEndpoint) {
  btnAddEndpoint.addEventListener('click', (e) => {
    e.stopPropagation(); // Prevent the settings section from collapsing
    if (!state.api_router) state.api_router = { endpoints: [], agent_priorities: {} };
    if (!state.api_router.endpoints) state.api_router.endpoints = [];
    
    // Add a new blank endpoint with defaults matching backend dataclass
    state.api_router.endpoints.push({
      id: crypto.randomUUID(),
      name: 'New Endpoint',
      api_base: 'http://localhost:1234/v1',
      api_key: 'EMPTY',
      model: '',
      enabled: true,
      max_retries: 2,
      // NEW defaults matching the backend dataclass
      base_retry_delay: 1.0,
      max_retry_delay: 30.0,
      rate_limit_rpm: 0
    });
    
    sendApiRouterUpdate();
  });
}
