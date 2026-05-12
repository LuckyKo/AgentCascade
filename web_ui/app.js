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

// ── Constants ────────────────────────────────────────────────────────────────
const USER = 'user';
const ASSISTANT = 'assistant';
const SYSTEM = 'system';
const FUNCTION = 'function';

// ── State ────────────────────────────────────────────────────────────────────
const state = {
  messages: [],
  subAgents: {},
  activeStack: [],
  approvals: [],
  generating: false,
  agents: [],
  agentIndex: 0,
  sessionName: localStorage.getItem('agent-cascade-session-name') || localStorage.getItem('qwen-session-name') || 'Maine',
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
    lastSubAgentRender: 0,      // For renderSubAgents throttling (~750ms)
  },
  totalTokens: 0,
  totalWords: 0,
  maxTokens: 32768,
  autoSecurity: false,
  summary: "", // Active compression summary
};

let ws = null;
let reconnectTimer = null;
let lastRenderedCount = Infinity;
let lastLastContent = null;

// ── DOM refs ─────────────────────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const messagesEl = $('#messages');
const chatInput = $('#chatInput');
const sendBtn = $('#sendBtn');
const continueBtn = $('#continueBtn');
const stopBtn = $('#stopBtn');
const resetBtn = $('#resetBtn');
const agentSelect = $('#agentSelect');
const sessionNameInput = $('#sessionName');
const statusText = $('#statusText');
const connectionDot = $('#connectionDot');
const approvalBar = $('#approvalBar');
const mainTabBar = $('#mainTabBar');
const mainTabChat = $('#mainTabChat');
const mainTabPanels = document.querySelector('.main-tab-panels');

// New CWrite-style DOM refs
const btnToggleSettings = $('#btn-toggle-settings');
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

const workAccessFoldersRW = $('#workAccessFoldersRW');
const workAccessFoldersRO = $('#workAccessFoldersRO');
const defaultWorkspace = $('#defaultWorkspace');

const settingVisionEnabled = $('#setting-vision-enabled');
const settingImageDetail = $('#setting-image-detail');
const settingMaxImageSize = $('#setting-max-image-size');
const insertImageBtn = $('#insertImageBtn');
const imageInput = $('#imageInput');

const settingMcpServers = $('#setting-mcp-servers');

const afkToggle = $('#afkToggle');
const autoSecurityToggle = $('#autoSecurityToggle');
const settingAfkMessage = $('#setting-afk-message');
const settingSummaryText = $('#setting-summary-text');
const settingSummaryAgentSelect = $('#setting-summary-agent-select');
const refreshMemoryBtn = $('#refresh-memory-btn');
const saveSummaryBtn = $('#save-summary-btn');

// Range outputs
const ranges = [
  { input: $('#setting-temperature'), output: $('#val-temperature') },
  { input: $('#setting-top-p'), output: $('#val-top-p') },
  { input: $('#setting-top-k'), output: $('#val-top-k') },
  { input: $('#setting-min-p'), output: $('#val-min-p') },
  { input: $('#setting-repeat-penalty'), output: $('#val-repeat-penalty') },
  { input: $('#setting-presence-penalty'), output: $('#val-presence-penalty') },
  { input: $('#setting-frequency-penalty'), output: $('#val-frequency-penalty') },
  { input: $('#setting-read-file-limit'), output: $('#val-read-file-limit') },
  { input: $('#setting-grep-char-limit'), output: $('#val-grep-char-limit') },
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
    <div class="session-item" data-path="${s.path.replace(/\\/g, '/')}">
      <div class="session-item-header">
        <span class="session-item-name">${s.name}</span>
        <span class="session-item-agent">${s.agent}</span>
      </div>
      <div class="session-item-meta">
        <span>${formatDate(s.mtime * 1000)}</span>
        <span>${formatSize(s.size)}</span>
      </div>
    </div>
  `).join('');

  // Add click listeners to session items
  document.querySelectorAll('.session-item').forEach(item => {
    item.addEventListener('click', () => {
      const path = item.dataset.path;
      loadSession(path);
    });
  });
}

function loadSession(path) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    if (confirm('Load this session? Current unsaved state will be lost.')) {
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
    renderMessages();
    renderSubAgents();
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

  if (settingImageDetail) s['setting-image-detail'] = settingImageDetail.value;
  if (settingMaxImageSize) s['setting-max-image-size'] = settingMaxImageSize.value;
  if (settingMcpServers) s['setting-mcp-servers'] = settingMcpServers.value;

  if (workAccessFoldersRW) s['work-access-folders-rw'] = workAccessFoldersRW.value;
  if (workAccessFoldersRO) s['work-access-folders-ro'] = workAccessFoldersRO.value;

  ranges.forEach(r => {
    if (r.input) s[r.input.id] = r.input.value;
  });

  if ($('#setting-max-turns')) s['max-turns'] = $('#setting-max-turns').value;
  if ($('#setting-auto-continue')) s['auto-continue'] = $('#setting-auto-continue').checked;
  if ($('#setting-read-file-limit')) s['read-file-limit'] = $('#setting-read-file-limit').value;
  if (settingVisionEnabled) s['vision-enabled'] = settingVisionEnabled.checked;
  if (afkToggle) s['afk-enabled'] = afkToggle.checked;
  if (settingAfkMessage) s['afk-message'] = settingAfkMessage.value;
  if (autoSecurityToggle) s['auto-security'] = autoSecurityToggle.checked;

  localStorage.setItem('agent-cascade-settings', JSON.stringify(s));
  
  if (state.connected) {
    send({ type: 'update_config', generate_cfg: getGenerateCfg() });
  }
  
  // Re-render to apply setting changes immediately (like context bar max value)
  renderMessages();
  renderSubAgents();
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

    if (s['vision-enabled'] !== undefined) $('#setting-vision-enabled').checked = s['vision-enabled'];
    if (s['max-turns'] !== undefined) $('#setting-max-turns').value = s['max-turns'];
    if (s['auto-continue'] !== undefined) $('#setting-auto-continue').checked = s['auto-continue'];
    if (s['read-file-limit'] !== undefined) {
      $('#setting-read-file-limit').value = s['read-file-limit'];
      $('#setting-read-file-limit').dispatchEvent(new Event('input'));
    }

    if (settingImageDetail && s['setting-image-detail'] !== undefined) {
      settingImageDetail.value = s['setting-image-detail'];
    }
    if (settingMaxImageSize && s['setting-max-image-size'] !== undefined) {
      settingMaxImageSize.value = s['setting-max-image-size'];
    }

    if (settingMcpServers && s['setting-mcp-servers'] !== undefined) {
      settingMcpServers.value = s['setting-mcp-servers'];
    }

    if (workAccessFoldersRW) {
      if (s['work-access-folders-rw'] !== undefined) {
        workAccessFoldersRW.value = s['work-access-folders-rw'];
      } else if (s['work-access-folders'] !== undefined) {
        // Migration from legacy
        workAccessFoldersRW.value = s['work-access-folders'];
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
    if (autoSecurityToggle.checked) {
      renderApprovals();
    }
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
    } catch { }
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

  switch (data.type) {
    case 'state':
    case 'done':
      // Full state update
      state.messages = data.messages || [];
      state.subAgents = data.sub_agents || {};
      state.activeStack = data.active_stack || [];
      state.generating = data.generating ?? false;
      if (data.agents) {
        state.agents = data.agents;
        renderAgentSelect();
      }
      if (data.session_name) state.sessionName = data.session_name;
      if (data.agent_index !== undefined) state.agentIndex = data.agent_index;
      if (data.approvals) {
        state.approvals = data.approvals;
        renderApprovals();
      }

      if (data.total_tokens !== undefined) state.totalTokens = data.total_tokens;
      if (data.total_words !== undefined) state.totalWords = data.total_words;
      if (data.max_tokens !== undefined) state.maxTokens = data.max_tokens;
      if (data.summary !== undefined) state.summary = data.summary;

      // Telemetry: update panel with session telemetry from server
      if (data.telemetry) {
        updateTelemetryPanel(data.telemetry);
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
      lastRenderedCount = Infinity;
      lastLastContent = null;
      renderMessages();
      renderSubAgents();
      updateControls();

      // Update stats if generating
      if (state.generating) {
        updateGenStats(state.messages);
      } else if (wasGenerating) {
        // Final update for stats
        updateGenStats(state.messages, true);
        state.genStats.active = false;
        
        // Refresh telemetry config comparison after turn ends
        fetchTelemetry();
      }
      
      updateMemoryTab();
      break;

    case 'stream_update': {
      // Lightweight streaming delta — only response messages + sub-agents
      const historyCount = data.history_count || 0;
      const responseMsgs = data.response_messages || [];

      // Merge: keep stable history, replace streaming response tail
      if (historyCount <= state.messages.length) {
        state.messages.length = historyCount;
      }
      state.messages.push(...responseMsgs);

      const oldStackStr = (state.activeStack || []).join(',');
      if (data.sub_agents) state.subAgents = data.sub_agents;
      if (data.active_stack) state.activeStack = data.active_stack;
      state.generating = true;
      const newStackStr = (state.activeStack || []).join(',');
      const stackChanged = oldStackStr !== newStackStr;

      // Update scalar stats (always lightweight — no DOM work)
      if (data.total_tokens !== undefined) state.totalTokens = data.total_tokens;
      if (data.total_words !== undefined) state.totalWords = data.total_words;
      if (data.max_tokens !== undefined) state.maxTokens = data.max_tokens;
      if (data.current_model && statusModel) statusModel.textContent = data.current_model;
      if (data.telemetry) updateTelemetryPanel(data.telemetry);

      // Approvals require immediate rendering (user must see these promptly)
      if (data.approvals) {
        state.approvals = data.approvals;
        renderApprovals();
      }

      // Always update message display — already incremental-optimized inside renderMessages()
      renderMessages();

      // Update UI controls (stop button, send disabled state, etc.)
      updateControls();

      // Throttle sub-agent rendering to ~750ms to match server refresh rate.
      // The server only updates sub_agents every ~5 ticks (~750ms), so rendering
      // more frequently is wasted work. Active flags (state.activeStack) are still
      // updated on every tick above, keeping the activity feed responsive.
      const now = performance.now();
      if (!state.genStats.lastSubAgentRender) state.genStats.lastSubAgentRender = 0;
      if (stackChanged || now - state.genStats.lastSubAgentRender > 750) {
        renderSubAgents();
        state.genStats.lastSubAgentRender = now;
        
        if (stackChanged) {
          if (state.activeStack.length > 0) {
            const topAgent = state.activeStack[state.activeStack.length - 1];
            // Only auto-switch if the sub-agent panel has actually been created
            if (state.subAgents && state.subAgents[topAgent] && state.activeSubTab !== 'sub-' + topAgent) {
              switchMainTab('sub-' + topAgent);
            }
          } else {
            switchMainTab('chat');
          }
        }
      }

      // Throttle gen stats to ~2Hz instead of ~6.5Hz. The token/sec display is
      // approximate anyway, so updating twice per second is visually indistinguishable
      // from the original frequency.
      if (!state.genStats.lastGenStatsUpdate) state.genStats.lastGenStatsUpdate = 0;
      if (now - state.genStats.lastGenStatsUpdate > 500) {
        updateGenStats(state.messages);
        state.genStats.lastGenStatsUpdate = now;
      }
    }
    break;

    case 'approvals':
      state.approvals = data.approvals || [];
      renderApprovals();
      break;

    case 'security_response': {
      const { request_id, response, verdict, reason } = data;
      const card = document.querySelector(`.approval-card[data-request-id="${request_id}"]`);
      if (card) {
          let respDiv = card.querySelector('.security-response-box');
          if (!respDiv) {
             respDiv = document.createElement('div');
             respDiv.className = 'security-response-box';
             respDiv.style = "margin-top: 8px; padding: 8px; background: rgba(255,193,7,0.15); border-left: 3px solid #ffc107; font-size: 13px; color: var(--text-color);";
             card.insertBefore(respDiv, card.querySelector('.approval-actions'));
          }
          respDiv.innerHTML = `<strong>🛡️ Security Expert:</strong><div style="margin-top:4px;">${renderMarkdown(response)}</div>`;
          
          const askBtn = card.querySelector('.ask-security-btn');
          if (askBtn) {
             askBtn.innerHTML = '🛡️ Ask Security';
             askBtn.disabled = false;
          }

          // QoL: If security advisor said NO, auto-fill the rejection field
          if (verdict === 'NO' && reason) {
              const rejectBtn = card.querySelector('.btn-danger');
              if (rejectBtn) {
                  // Only show input if not already visible
                  if (!card.querySelector('.reject-input-area')) {
                      showRejectInput(request_id, rejectBtn);
                  }
                  // Find the input within this specific card
                  const input = card.querySelector('.reject-reason-input');
                  if (input) {
                      input.value = reason;
                  }
              }
          }
      }
      break;
    }

    case 'error':
      state.generating = false;
      appendSystemBubble(`⚠️ Error: ${data.message}`);
      updateControls();
      break;
  }

  // Trigger sounds based on state changes
  const newApprovalsCount = (state.approvals || []).length;
  if (newApprovalsCount > prevApprovalsCount) {
    playSound('intervention');
  } else if (wasGenerating && !state.generating) {
    playSound('completed');
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
  sendMessage();
}

// ── Rendering ────────────────────────────────────────────────────────────────

function renderMessages() {
  const msgs = state.messages;
  const container = messagesEl;

  updateContextBar(document.getElementById('chatContextFill'), msgs, state.totalTokens, state.maxTokens);

  // Word count and token estimation from Backend
  if (statusWords) {
    statusWords.textContent = `${state.totalWords} words`;
  }
  if (statusTokens) {
    statusTokens.textContent = `${state.totalTokens} tokens`;
  }

  // Quick check: if nothing meaningful changed, skip heavy re-render
  const currentCount = msgs.length;
  const lastMsg = msgs.length > 0 ? msgs[msgs.length - 1] : null;
  const lastContent = lastMsg ? (lastMsg.content || '') + (lastMsg.function_call ? JSON.stringify(lastMsg.function_call) : '') + (lastMsg.reasoning_content || '') : '';

  // Full re-render if count changed significantly or decreased
  if (currentCount < lastRenderedCount || currentCount === 0) {
    fullRender(msgs, container);
    lastRenderedCount = currentCount;
    lastLastContent = lastContent;
    return;
  }

  // Append new messages
  if (currentCount > lastRenderedCount) {
    for (let i = lastRenderedCount; i < currentCount; i++) {
      container.appendChild(createMessageEl(msgs[i], i));
    }
    lastRenderedCount = currentCount;
  }

  // Auto-scroll logic: only scroll if was at bottom before update
  const wasAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 50;

  // Update last message content (streaming)
  if (lastContent !== lastLastContent && container.lastElementChild) {
    const lastBubble = container.lastElementChild;
    const idx = parseInt(lastBubble.dataset.index);
    if (idx === currentCount - 1 && state.editingIndex !== idx) {
      updateBubbleContent(lastBubble, msgs[currentCount - 1]);
    }
  }
  lastLastContent = lastContent;

  if (wasAtBottom) {
    scrollToBottom();
  }

  // Update main activity bar
  updateMainActivityBar();
}

function updateMainActivityBar() {
  const bar = document.getElementById('mainActivityBar');
  if (!bar) return;

  const activityText = bar.querySelector('.activity-text');
  const chatTab = document.getElementById('mainTabChat');

  if (state.generating) {
    bar.classList.add('active');
    if (chatTab) chatTab.classList.add('agent-active');

    const msgs = state.messages || [];
    const lastMsg = msgs.length > 0 ? msgs[msgs.length - 1] : null;
    if (lastMsg) {
      activityText.textContent = getActivityPreview(lastMsg);
    } else {
      activityText.textContent = 'Agent Starting...';
    }
    if (state.totalTokens !== undefined) {
      activityText.textContent += ` (${state.totalWords} words, ${state.totalTokens} tokens)`;
    }
  } else {
    bar.classList.remove('active');
    if (chatTab) chatTab.classList.remove('agent-active');
    activityText.textContent = 'Agent Idle';
    if (state.totalTokens !== undefined) {
      activityText.textContent += ` (${state.totalWords} words, ${state.totalTokens} tokens)`;
    }
  }

  // Also update main chat tab
  if (chatTab) {
    chatTab.innerHTML = `<span class="main-tab-icon">💬</span> Chat`;
  }
}

function fullRender(msgs, container) {
  container.innerHTML = '';
  for (let i = 0; i < msgs.length; i++) {
    if (msgs[i].role === 'system') continue; // Hide system messages
    container.appendChild(createMessageEl(msgs[i], i));
  }
  scrollToBottom();
}

function createMessageEl(msg, index) {
  const div = document.createElement('div');
  div.className = `message msg-${msg.role || 'unknown'}`;
  div.dataset.index = index;

  const isEditable = !msg.function_call && msg.role !== 'function' && msg.role !== 'system';

  // Header
  const header = document.createElement('div');
  header.className = 'msg-header';
  const nameSpan = document.createElement('span');
  nameSpan.className = 'msg-name';
  if (msg.role === 'user') {
    nameSpan.textContent = 'You';
  } else if (msg.role === 'function') {
    nameSpan.textContent = `✅ ${msg.name || 'Tool Result'}`;
  } else {
    nameSpan.textContent = msg.name || 'Assistant';
  }
  header.appendChild(nameSpan);

  // Actions
  const actions = document.createElement('div');
  actions.className = 'msg-actions';

  if (isEditable) {
    const editBtn = document.createElement('button');
    editBtn.className = 'msg-action-btn';
    editBtn.textContent = '✏️';
    editBtn.title = 'Edit message';
    editBtn.onclick = (e) => { e.stopPropagation(); startEdit(index); };
    actions.appendChild(editBtn);
  }

  const delBtn = document.createElement('button');
  delBtn.className = 'msg-action-btn msg-action-delete';
  delBtn.textContent = '🗑️';
  delBtn.title = 'Delete message';
  delBtn.onclick = (e) => { e.stopPropagation(); deleteMessage(index); };
  actions.appendChild(delBtn);

  header.appendChild(actions);

  div.appendChild(header);

  // Double click edit
  div.addEventListener('dblclick', (e) => {
    if (state.generating || !isEditable) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;

    let selectedText = sel.toString().trim();
    if (!selectedText) return;

    if (e.target.closest('.msg-header')) return;

    const contentDiv = div.querySelector('.msg-content');
    if (!contentDiv) return;

    const range = sel.getRangeAt(0);
    const preCaretRange = range.cloneRange();
    preCaretRange.selectNodeContents(contentDiv);
    preCaretRange.setEnd(range.startContainer, range.startOffset);
    const renderedOffset = preCaretRange.toString().length;
    const renderedLength = contentDiv.textContent.length;

    const proportion = renderedLength > 0 ? renderedOffset / renderedLength : 0;

    startEdit(index, selectedText, proportion);
  });

  // Content
  const contentDiv = document.createElement('div');
  contentDiv.className = 'msg-content';

  let html = '';
  const isGenerating = state.generating && index === state.messages.length - 1;

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
    if (msg.reasoning_content && text.includes('<think>')) {
        const thinkMatch = text.match(/<think>([\s\S]*?)(<\/think>|$)/i);
        if (thinkMatch) {
            const embedded = thinkMatch[1].trim();
            const reasoning = msg.reasoning_content.trim();
            if (reasoning.includes(embedded) || embedded.includes(reasoning)) {
                // Remove the redundant block from content
                text = (text.substring(0, thinkMatch.index) + text.substring(thinkMatch.index + thinkMatch[0].length)).trim();
            }
        }
    }
    
    html += renderMarkdown(text);
  }

  contentDiv.innerHTML = html;

  div.appendChild(contentDiv);
  return div;
}

function updateBubbleContent(bubble, msg) {
  const contentDiv = bubble.querySelector('.msg-content');
  if (!contentDiv) return;

  let html = '';
  const isGenerating = state.generating;

  if (msg.reasoning_content) {
    html += renderThinkingBlock(msg.reasoning_content, isGenerating);
  }

  if (msg.function_call) {
    html += renderToolCall(msg);
  } else if (msg.role === 'function') {
    html += renderToolResult(msg);
  } else {
    let text = msg.content || '';
    if (msg.reasoning_content && text.includes('<think>')) {
        const thinkMatch = text.match(/<think>([\s\S]*?)(<\/think>|$)/i);
        if (thinkMatch) {
            const embedded = thinkMatch[1].trim();
            const reasoning = msg.reasoning_content.trim();
            if (reasoning.includes(embedded) || embedded.includes(reasoning)) {
                text = (text.substring(0, thinkMatch.index) + text.substring(thinkMatch.index + thinkMatch[0].length)).trim();
            }
        }
    }
    html += renderMarkdown(text);
  }
  setInnerHtmlWithState(contentDiv, html);
}

function setInnerHtmlWithState(el, html) {
  const details = el.querySelectorAll('details');
  const states = Array.from(details).map(d => d.open);

  el.innerHTML = html;

  const newDetails = el.querySelectorAll('details');
  newDetails.forEach((d, i) => {
    if (i < states.length) {
      d.open = states[i];
    }
  });
}

function renderMarkdown(text) {
  if (!text || !text.trim()) return '';

  // Handle <think> tags in content (fallback for models that don't use reasoning_content field)
  const thinkMatch = text.match(/<think>([\s\S]*?)(<\/think>|$)/);
  if (thinkMatch) {
    const thought = thinkMatch[1];
    const isOpen = !text.includes('</think>');
    const before = text.substring(0, text.indexOf('<think>'));
    const after = text.includes('</think>') ? text.substring(text.indexOf('</think>') + 8) : '';
    
    let html = '';
    if (before.trim()) html += renderMarkdown(before);
    html += renderThinkingBlock(thought, isOpen);
    if (after.trim()) html += renderMarkdown(after);
    return html;
  }

  try {
    return marked.parse(text);
  } catch {
    return `<p>${escapeHtml(text)}</p>`;
  }
}

function renderToolCall(msg) {
  const fc = msg.function_call;
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

function renderToolResult(msg) {
  const content = msg.content || '';
  const shouldTruncate = settingTruncateTools ? settingTruncateTools.checked : true;
  const truncated = (shouldTruncate && content.length > 2000) ? content.substring(0, 2000) + '\n\n... (truncated)' : content;

  let contentHtml = `<pre><code>${escapeHtml(truncated)}</code></pre>`;
  if (msg.name === 'view_image' || content.match(/!\[.*?\]\(.*?\)/)) {
    // Rewrite file:/// URLs to use our backend proxy to avoid browser security restrictions
    const proxiedContent = truncated.replace(/!\[(.*?)\]\((?:file:\/\/\/|file:\/\/)(.*?)\)/g, '![image](/api/file?path=$2)');
    contentHtml = `<div class="tool-image-wrapper" style="padding-top: 8px;">${renderMarkdown(proxiedContent)}</div>`;
  }

  return `
    <details class="tool-result">
      <summary>📋 Result from <strong>${escapeHtml(msg.name || 'tool')}</strong>${shouldTruncate && content.length > 2000 ? ` <span class="truncation-hint">(${content.length.toLocaleString()} chars)</span>` : ''}</summary>
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

function appendSystemBubble(text) {
  const div = document.createElement('div');
  div.className = 'message msg-system';
  div.innerHTML = `<div class="msg-content">${renderMarkdown(text)}</div>`;
  messagesEl.appendChild(div);
  scrollToBottom();
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  });
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

function startEdit(index, selectedText = '', proportion = 0) {
  const msg = state.messages[index];
  if (!msg || msg.function_call || msg.role === 'function' || state.generating) return;

  state.editingIndex = index;

  const bubble = messagesEl.querySelector(`.message[data-index="${index}"]`);
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
  saveBtn.textContent = '✓ Save';
  saveBtn.onclick = () => finishEdit(index, textarea.value);

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'btn btn-secondary btn-sm';
  cancelBtn.textContent = '✗ Cancel';
  cancelBtn.onclick = () => cancelEdit(index);

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
      finishEdit(index, textarea.value);
    } else if (e.key === 'Escape') {
      cancelEdit(index);
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

function finishEdit(index, newContent) {
  if (state.editingIndex === index) state.editingIndex = null;
  state.messages[index].content = newContent; // Optimistic update
  send({ type: 'edit_message', index, content: newContent });

  // Localized re-render
  const bubble = messagesEl.querySelector(`.message[data-index="${index}"]`);
  if (!bubble) return;
  bubble.querySelector('.msg-content').classList.remove('editing');
  updateBubbleContent(bubble, state.messages[index]);
}

function cancelEdit(index) {
  if (state.editingIndex === index) state.editingIndex = null;

  // Localized re-render
  const bubble = messagesEl.querySelector(`.message[data-index="${index}"]`);
  if (!bubble) return;
  bubble.querySelector('.msg-content').classList.remove('editing');
  updateBubbleContent(bubble, state.messages[index]);
}

function deleteMessage(index) {
  const msg = state.messages[index];
  if (!msg) return;

  // If deleting an assistant message with a function_call, also delete the function result
  const indicesToDelete = [index];
  if (msg.function_call && index + 1 < state.messages.length && state.messages[index + 1].role === 'function') {
    indicesToDelete.push(index + 1);
  }
  // If deleting a function result, also delete the preceding function call
  if (msg.role === 'function' && index - 1 >= 0 && state.messages[index - 1].function_call) {
    indicesToDelete.push(index - 1);
  }

  send({ type: 'delete_messages', indices: [...new Set(indicesToDelete)] });
}

// ── Approvals ────────────────────────────────────────────────────────────────

function renderApprovals() {
  const bar = approvalBar;
  if (!state.approvals || state.approvals.length === 0) {
    bar.style.display = 'none';
    return;
  }

  // Auto-security check (Auto-Ask) takes priority
  if (state.autoSecurity) {
    const pending = [...state.approvals];
    state.approvals = [];
    bar.style.display = 'none';
    
    pending.forEach(ap => {
      send({ type: 'ask_security', request_id: ap.request_id, auto_apply: true });
    });
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

    card.innerHTML = `
      <div class="approval-header">
        <span class="approval-icon">🛡️</span>
        <strong>Approval Required</strong>
      </div>
      <div class="approval-meta">
        <span>Agent: <strong>${escapeHtml(ap.agent_name)}</strong></span>
        <span>Tool: <strong>${escapeHtml(ap.tool_name)}</strong></span>
      </div>
      <div class="approval-desc">${renderMarkdown(ap.description)}</div>
      <details class="approval-args">
        <summary>Raw Arguments</summary>
        <pre><code>${argsHtml}</code></pre>
      </details>
      <div class="approval-actions">
        <button class="btn btn-primary btn-sm" onclick="approveRequest('${ap.request_id}')">✅ Approve</button>
        <button class="btn btn-warning btn-sm ask-security-btn" onclick="askSecurity('${ap.request_id}', this)">🛡️ Ask Security</button>
        <button class="btn btn-danger btn-sm" onclick="showRejectInput('${ap.request_id}', this)">❌ Reject</button>
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
    <input type="text" placeholder="Rejection reason..." class="reject-reason-input" id="reject-${requestId}">
    <button class="btn btn-danger btn-sm" onclick="rejectRequest('${requestId}')">Confirm Reject</button>
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
  const sa = state.subAgents;
  const names = Object.keys(sa);

  // Remove stale sub-agent tabs and panels for agents that no longer exist
  mainTabBar.querySelectorAll('.main-tab[data-tab^="sub-"]').forEach(tab => {
    const agentName = tab.dataset.tab.substring(4);
    if (!names.includes(agentName)) {
      tab.remove();
      const panel = document.getElementById('panelSub-' + agentName);
      if (panel) panel.remove();
    }
  });

  if (names.length === 0) return;

  // Auto-select active tab from stack
  const activeTop = state.activeStack.length > 0 ? state.activeStack[state.activeStack.length - 1] : null;

  for (const name of names) {
    const tabId = 'sub-' + name;
    const isActive = sa[name].active;

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

      const closeBtn = document.createElement('span');
      closeBtn.className = 'close-tab';
      closeBtn.title = 'Terminate Agent';
      closeBtn.textContent = '\u00d7';
      closeBtn.onclick = (e) => {
        e.stopPropagation();
        send({ type: 'terminate_sub_agent', instance_name: name });
        switchMainTab('chat');
      };
      tabBtn.appendChild(closeBtn);

      mainTabBar.appendChild(tabBtn);
    }

    // Update tab content safely (preserves handlers on closeBtn)
    const iconSpan = tabBtn.querySelector('.tab-icon-container');
    if (iconSpan) {
      iconSpan.innerHTML = isActive ? '<span class="sub-tab-pulse"></span>' : '<span class="main-tab-icon">🤖</span>';
    }
    const labelSpan = tabBtn.querySelector('.tab-label');
    if (labelSpan) {
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

    // Ensure input area exists for sub-agent direct interaction
    let inputArea = panel.querySelector('.input-area');
    if (!inputArea) {
      inputArea = document.createElement('div');
      inputArea.className = 'input-area';
      inputArea.innerHTML = `
        <div class="input-wrapper">
          <textarea placeholder="Message ${name}..." rows="1"></textarea>
          <div class="sub-input-btns" style="display: flex; gap: 4px; align-items: center;">
            <button class="btn btn-secondary sub-continue-btn" title="Continue (Ctrl+Shift+Enter)" style="padding: 6px 8px; font-size: 12px;">⏩</button>
            <button class="btn btn-primary send-btn" title="Send (Enter)">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor">
                <path d="M2 21l21-9L2 3v7l15 2-15 2z" />
              </svg>
            </button>
          </div>
        </div>
      `;
      const textarea = inputArea.querySelector('textarea');
      const sendBtn = inputArea.querySelector('.send-btn');
      const contBtn = inputArea.querySelector('.sub-continue-btn');

      textarea.addEventListener('input', () => autoResize(textarea));
      textarea.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          sendMessage(textarea);
        } else if (e.key === 'Enter' && e.shiftKey && e.ctrlKey) {
          e.preventDefault();
          continueMessage();
        }
      });
      sendBtn.onclick = () => sendMessage(textarea);
      if (contBtn) contBtn.onclick = () => continueMessage();

      // We'll append it before the activity bar in renderSubAgentPanel
    }

    // Render sub-agent messages into the panel
    renderSubAgentPanel(panel, sa[name], name);

    // Update tab activity dot
    if (tabBtn) {
      if (sa[name].active) {
        tabBtn.classList.add('agent-active');
      } else {
        tabBtn.classList.remove('agent-active');
      }
    }
  }
}

function renderSubAgentPanel(panel, agentData, name) {
  const msgs = agentData.messages || [];

  const fillEl = document.getElementById('subContextFill-' + name);
  if (fillEl) {
    updateContextBar(fillEl, msgs, agentData.total_tokens, agentData.max_tokens);
  }

  // 1. Ensure scroll container exists
  let scrollContainer = panel.querySelector('.sub-agent-messages');
  if (!scrollContainer) {
    scrollContainer = document.createElement('div');
    scrollContainer.className = 'sub-agent-messages';
    panel.appendChild(scrollContainer);
  }

  // 2. Ensure activity bar exists and is at the bottom
  let activityBar = panel.querySelector('.sub-agent-activity-bar');
  if (!activityBar) {
    activityBar = document.createElement('div');
    activityBar.className = 'sub-agent-activity-bar';
    activityBar.innerHTML = `
      <div class="activity-status">
        <span class="activity-dot"></span>
        <span>Activity</span>
      </div>
      <div class="activity-text">Idle</div>
      <button class="btn btn-danger btn-sm terminate-btn" style="margin-left: auto; display: none; padding: 2px 8px; font-size: 11px;">Terminate</button>
    `;
    panel.appendChild(activityBar);
  }

  const terminateBtn = activityBar.querySelector('.terminate-btn');
  terminateBtn.onclick = () => {
    send({ type: 'terminate_sub_agent', instance_name: name });
    // Return focus to main chat tab
    switchMainTab('chat');
  };

  // 3. Ensure input area is present and correctly ordered
  let inputArea = panel.querySelector('.input-area');
  if (!inputArea) {
    // If not created in renderSubAgents (first run), create now
    inputArea = document.createElement('div');
    inputArea.className = 'input-area';
    inputArea.innerHTML = `
      <div class="input-wrapper">
        <textarea placeholder="Message ${name}..." rows="1"></textarea>
        <div class="sub-input-btns" style="display: flex; gap: 4px; align-items: center;">
          <button class="btn btn-secondary sub-continue-btn" title="Continue (Ctrl+Shift+Enter)" style="padding: 6px 8px; font-size: 12px;">⏩</button>
          <button class="btn btn-primary send-btn" title="Send (Enter)">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor">
              <path d="M2 21l21-9L2 3v7l15 2-15 2z" />
            </svg>
          </button>
        </div>
      </div>
    `;
    const textarea = inputArea.querySelector('textarea');
    const sendBtn = inputArea.querySelector('.send-btn');
    const contBtn = inputArea.querySelector('.sub-continue-btn');

    textarea.addEventListener('input', () => autoResize(textarea));
    textarea.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage(textarea);
      } else if (e.key === 'Enter' && e.shiftKey && e.ctrlKey) {
        e.preventDefault();
        continueMessage();
      }
    });
    sendBtn.onclick = () => sendMessage(textarea);
    if (contBtn) contBtn.onclick = () => continueMessage();
    panel.insertBefore(inputArea, activityBar);
  } else {
    // Ensure it's before activity bar
    if (inputArea.nextSibling !== activityBar) {
      panel.insertBefore(inputArea, activityBar);
    }
  }

  // 4. Always update activity bar status
  const activityText = activityBar.querySelector('.activity-text');
  const lastMsg = msgs.length > 0 ? msgs[msgs.length - 1] : null;
  if (agentData.active) {
    activityBar.classList.add('active');
    if (lastMsg) {
      activityText.textContent = getActivityPreview(lastMsg);
    } else {
      activityText.textContent = 'Agent Starting...';
    }
    // Update sub-agent stats in activity bar
    if (agentData.total_tokens !== undefined) {
      activityText.textContent += ` (${agentData.total_words} words, ${agentData.total_tokens} tokens)`;
    }
    terminateBtn.style.display = 'block';
  } else {
    activityBar.classList.remove('active');
    activityText.textContent = 'Agent Idle';
    terminateBtn.style.display = 'none';
  }

  const wasAtBottom = scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight < 50;

  // 4. Only re-render messages if content changed
  const lastMsgTextLen = (() => {
    if (!lastMsg) return 0;
    if (Array.isArray(lastMsg.content)) {
      return lastMsg.content.reduce((sum, item) => sum + (item.text ? String(item.text).length : 0), 0);
    }
    return String(lastMsg.content || '').length;
  })();
  const funcCallLen = (lastMsg && lastMsg.function_call && lastMsg.function_call.arguments) ? String(lastMsg.function_call.arguments).length : 0;
  const contentKey = msgs.length + ':' + lastMsgTextLen + ':' + (lastMsg ? String(lastMsg.reasoning_content || '').length : 0) + ':' + funcCallLen;
  if (panel.dataset.contentKey === contentKey) {
    if (wasAtBottom) scrollContainer.scrollTop = scrollContainer.scrollHeight;
    return;
  }
  panel.dataset.contentKey = contentKey;

  // 5. Render messages incrementally
  const currentCount = msgs.length;
  const lastCount = parseInt(panel.dataset.lastRenderedCount || '0');

  if (currentCount < lastCount || lastCount === 0) {
    scrollContainer.innerHTML = '';
    for (let i = 0; i < currentCount; i++) {
      scrollContainer.appendChild(createSubMsgEl(msgs[i], agentData.active && i === currentCount - 1));
    }
  } else {
    // Append new messages
    for (let i = lastCount; i < currentCount; i++) {
      scrollContainer.appendChild(createSubMsgEl(msgs[i], agentData.active && i === currentCount - 1));
    }
    // Update the last message if it's still being generated
    if (scrollContainer.lastElementChild) {
      updateSubBubbleContent(scrollContainer.lastElementChild, msgs[currentCount - 1], agentData.active);
    }
  }
  panel.dataset.lastRenderedCount = currentCount;

  // 6. Final scroll
  if (wasAtBottom) scrollContainer.scrollTop = scrollContainer.scrollHeight;
}

function createSubMsgEl(msg, isGenerating) {
  const div = document.createElement('div');
  div.className = `sub-msg sub-msg-${msg.role || 'unknown'}`;

  const label = document.createElement('div');
  label.className = 'sub-msg-label';
  label.textContent = msg.role === 'user' ? '📤 Task' :
    msg.role === 'function' ? `📋 ${msg.name || 'result'}` :
      msg.name || 'Agent';

  const content = document.createElement('div');
  content.className = 'sub-msg-content';

  div.appendChild(label);
  div.appendChild(content);

  updateSubBubbleContent(div, msg, isGenerating);
  return div;
}

function updateSubBubbleContent(bubble, msg, isGenerating) {
  const content = bubble.querySelector('.sub-msg-content');
  if (!content) return;

  let html = '';
  if (msg.reasoning_content) {
    html += renderThinkingBlock(msg.reasoning_content, isGenerating);
  }

  if (msg.function_call) {
    html += renderToolCall(msg);
  } else if (msg.role === 'function') {
    html += renderToolResult(msg);
  } else {
    const textContent = msg.content || '';
    const thinkMatch = textContent.match(/<think>([\s\S]*?)(<\/think>|$)/);
    if (thinkMatch) {
      const thought = thinkMatch[1];
      const isOpen = !textContent.includes('</think>');
      const before = textContent.substring(0, textContent.indexOf('<think>'));
      const after = textContent.includes('</think>') ? textContent.substring(textContent.indexOf('</think>') + 8) : '';
      if (before.trim()) html += renderMarkdown(before);
      html += renderThinkingBlock(thought, isOpen);
      if (after.trim()) html += renderMarkdown(after);
    } else {
      html += renderMarkdown(textContent);
    }
  }

  setInnerHtmlWithState(content, html);
}

function switchMainTab(tabId) {
  // Update tab buttons
  mainTabBar.querySelectorAll('.main-tab').forEach(t => t.classList.remove('active'));
  const activeTab = mainTabBar.querySelector(`.main-tab[data-tab="${tabId}"]`);
  if (activeTab) activeTab.classList.add('active');

  // Update panels
  mainTabPanels.querySelectorAll('.main-tab-panel').forEach(p => p.classList.remove('active'));
  if (tabId === 'chat') {
    const chatPanel = document.getElementById('panelChat');
    chatPanel.classList.add('active');
    const scroll = chatPanel.querySelector('.messages-scroll');
    if (scroll) scroll.scrollTop = scroll.scrollHeight;
  } else {
    const name = tabId.substring(4); // strip 'sub-'
    const panel = document.getElementById('panelSub-' + name);
    if (panel) {
      panel.classList.add('active');
      const scroll = panel.querySelector('.messages-scroll');
      if (scroll) scroll.scrollTop = scroll.scrollHeight;
    }
  }
  
  state.activeSubTab = tabId;
  updateMemoryTab();
}

// Wire up the static Chat tab
if (mainTabChat) {
  mainTabChat.addEventListener('click', () => switchMainTab('chat'));
}

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
      if (agent.index === state.agentIndex) opt2.selected = true;
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

  settingToolsList.querySelectorAll('.tool-toggle').forEach(chk => {
    chk.addEventListener('change', (e) => {
      const aName = e.target.dataset.agent;
      const tName = e.target.dataset.tool;
      if (!agentDisabledTools[aName]) agentDisabledTools[aName] = [];
      if (!e.target.checked) {
        if (!agentDisabledTools[aName].includes(tName)) agentDisabledTools[aName].push(tName);
      } else {
        agentDisabledTools[aName] = agentDisabledTools[aName].filter(t => t !== tName);
      }
      localStorage.setItem('agent-cascade-disabled-tools', JSON.stringify(agentDisabledTools));
      saveSettings();
    });
  });
}

if (settingAgentSelect) {
  settingAgentSelect.addEventListener('change', renderToolsForSelectedAgent);
}

// Settings Tabs
document.querySelectorAll('.settings-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.settings-tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    const panel = document.getElementById(btn.dataset.tab);
    if (panel) panel.classList.add('active');
    
    if (btn.dataset.tab === 'settings-memory') {
      // On first open, default to active tab
      if (settingSummaryAgentSelect) {
         settingSummaryAgentSelect.value = state.activeSubTab || 'chat';
      }
      updateMemoryTab();
    }
  });
});

// ── Controls ─────────────────────────────────────────────────────────────────

function updateControls() {
  if (state.generating) {
    sendBtn.classList.add('inject-mode');
    sendBtn.title = 'Inject message into active agent (Enter)';
    if (mainTabChat) mainTabChat.innerHTML = '<span class="sub-tab-pulse"></span> Chat';
    resetBtn.disabled = true;
    document.body.classList.add('is-generating');
  } else {
    sendBtn.classList.remove('inject-mode');
    sendBtn.title = 'Send (Enter)';
    if (mainTabChat) mainTabChat.innerHTML = '<span class="main-tab-icon">💬</span> Chat';
    resetBtn.disabled = false;
    document.body.classList.remove('is-generating');
  }
  stopBtn.style.display = state.generating ? 'inline-flex' : 'none';
  sendBtn.disabled = !state.connected;
  continueBtn.disabled = state.generating || state.messages.length === 0;
  const refreshBtn = document.getElementById('refreshBtn');
  const mainRB = document.getElementById('mainRetryBtn');
  const retryDisabled = state.generating || state.messages.length === 0;
  if (refreshBtn) refreshBtn.disabled = state.generating;
  if (mainRB) mainRB.disabled = retryDisabled;

  statusText.textContent = state.generating ? 'Generating...' : '';
  chatInput.placeholder = state.generating
    ? 'Inject a message into the active agent...'
    : 'Send a message...';

  if (sessionNameInput && document.activeElement !== sessionNameInput) {
    sessionNameInput.value = state.sessionName;
  }
}

// ── Auto-resize textarea ─────────────────────────────────────────────────────

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 200) + 'px';
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

function updateContextBar(barEl, msgs, overrideTokens, overrideMax) {
  if (!barEl) return;

  const tokens = overrideTokens || 0;
  // Prioritize the UI setting for max context if it's available
  const maxContext = (settingMaxContext && settingMaxContext.value) ? parseInt(settingMaxContext.value) : (overrideMax || 32768);

  const pct = Math.min(100, Math.max(0, (tokens / maxContext) * 100));
  barEl.style.width = pct + '%';
  barEl.title = `${tokens} / ${maxContext} tokens`;

  if (pct > 90) {
    barEl.className = 'context-bar-fill danger';
  } else if (pct > 75) {
    barEl.className = 'context-bar-fill warning';
  } else {
    barEl.className = 'context-bar-fill';
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
    startMsgCount: state.messages ? state.messages.length : 0,
    saStartCounts: saStartCounts,
    tokenCount: 0,
    active: true,
    // Reset throttle timestamps at generation start for fresh timing windows
    lastGenStatsUpdate: 0,
    lastSubAgentRender: 0,
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

  // 2. Sub-Agent Tokens
  if (state.subAgents) {
    for (const name in state.subAgents) {
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

function updateMemoryTab(forceRebuild = false) {
  if (!settingSummaryText || !settingSummaryAgentSelect) return;
  
  // 1. Sync select options with current sub-agents
  const subAgentNames = Object.keys(state.subAgents || {}).sort();
  const optionValues = ['chat', ...subAgentNames.map(n => 'sub-' + n)];
  
  // Only rebuild options if list of sub-agents changed or forced
  const currentOptionValues = Array.from(settingSummaryAgentSelect.options).map(o => o.value);
  if (forceRebuild || JSON.stringify(optionValues) !== JSON.stringify(currentOptionValues)) {
    const prevVal = settingSummaryAgentSelect.value;
    settingSummaryAgentSelect.innerHTML = `
      <option value="chat">Main Orchestrator</option>
      ${subAgentNames.map(name => `<option value="sub-${name}">${escapeHtml(name)}</option>`).join('')}
    `;
    // Try to restore previous selection, or follow active tab if it's the first time
    if (optionValues.includes(prevVal)) {
      settingSummaryAgentSelect.value = prevVal;
    } else {
      settingSummaryAgentSelect.value = state.activeSubTab || 'chat';
    }
  }

  const selectedId = settingSummaryAgentSelect.value;
  
  if (selectedId === 'chat') {
    settingSummaryText.value = state.summary || "";
  } else if (selectedId.startsWith('sub-')) {
    const name = selectedId.substring(4);
    const sa = state.subAgents[name];
    settingSummaryText.value = (sa && sa.summary) ? sa.summary : "";
  }
}

if (refreshMemoryBtn) {
  refreshMemoryBtn.addEventListener('click', () => updateMemoryTab(true));
}

if (settingSummaryAgentSelect) {
  settingSummaryAgentSelect.addEventListener('change', () => updateMemoryTab(false));
}

// ── Image Handling ───────────────────────────────────────────────────────────

function insertImageMarkdown(base64Data, filename) {
  const markdown = `![${filename}](${base64Data})`;
  const startPos = chatInput.selectionStart;
  const endPos = chatInput.selectionEnd;
  const text = chatInput.value;
  chatInput.value = text.substring(0, startPos) + markdown + text.substring(endPos);
  chatInput.selectionStart = chatInput.selectionEnd = startPos + markdown.length;
  chatInput.focus();
  autoResize(chatInput);
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

if (insertImageBtn && imageInput) {
  insertImageBtn.addEventListener('click', () => imageInput.click());
  imageInput.addEventListener('change', (e) => {
    if (e.target.files && e.target.files.length > 0) {
      processImageFile(e.target.files[0]);
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
    Array.from(e.dataTransfer.files).forEach(processImageFile);
  }
});

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

chatInput.addEventListener('input', () => autoResize(chatInput));
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

sendBtn.addEventListener('click', sendMessage);
stopBtn.addEventListener('click', () => send({ type: 'stop' }));
const onRetryClick = () => {
  lastRenderedCount = Infinity;
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
    lastRenderedCount = Infinity;
    send({ type: 'reset' });
  }
});

agentSelect.addEventListener('change', () => {
  state.agentIndex = parseInt(agentSelect.value);
  send({ type: 'select_agent', index: state.agentIndex });
});

sessionNameInput.addEventListener('change', () => {
  state.sessionName = sessionNameInput.value.trim() || 'Maine';
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
  if ($('#setting-auto-continue')) cfg.auto_continue = $('#setting-auto-continue').checked;
  if ($('#setting-auto-rollback')) cfg.auto_rollback_on_loop = $('#setting-auto-rollback').checked;
  if ($('#setting-log-api-post')) cfg.log_api_post = $('#setting-log-api-post').checked;
  if ($('#setting-max-rollbacks')) cfg.max_auto_rollbacks = parseInt($('#setting-max-rollbacks').value);
  if ($('#setting-read-file-limit')) cfg.read_file_limit = parseInt($('#setting-read-file-limit').value) || 1000;
  if ($('#setting-grep-char-limit')) cfg.grep_char_limit = parseInt($('#setting-grep-char-limit').value);
  if ($('#setting-shell-char-limit')) cfg.shell_char_limit = parseInt($('#setting-shell-char-limit').value);
  if ($('#setting-code-char-limit')) cfg.code_char_limit = parseInt($('#setting-code-char-limit').value);

  if ($('#setting-mcp-servers') && $('#setting-mcp-servers').value.trim()) {
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

  if (state.generating) {
    // Async injection: message will be injected into the running agent
    send({ type: 'message', text });
    // Visual feedback
    const feedbackText = document.getElementById('statusText');
    if (feedbackText) {
      const prev = feedbackText.textContent;
      feedbackText.textContent = '⚡ Message injected';
      feedbackText.style.color = 'var(--accent)';
      setTimeout(() => {
        feedbackText.textContent = prev;
        feedbackText.style.color = '';
      }, 1500);
    }
    return;
  }

  resetGenStats();
  send({
    type: 'message',
    text,
    agent_index: state.agentIndex,
    session_name: state.sessionName,
    generate_cfg: getGenerateCfg()
  });
}

function continueMessage() {
  if (state.generating) return;

  const text = "[SYSTEM]: Please continue.";
  resetGenStats();
  send({
    type: 'message',
    text,
    agent_index: state.agentIndex,
    session_name: state.sessionName,
    generate_cfg: getGenerateCfg()
  });
}

function retryGeneration() {
  if (state.generating) return;
  resetGenStats();
  send({
    type: 'retry',
    agent_index: state.agentIndex,
    session_name: state.sessionName,
    generate_cfg: getGenerateCfg()
  });
}

// ── Init ─────────────────────────────────────────────────────────────────────
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

if (saveSummaryBtn) {
  saveSummaryBtn.addEventListener('click', () => {
    const content = settingSummaryText.value;
    const selectedId = settingSummaryAgentSelect ? settingSummaryAgentSelect.value : (state.activeSubTab || 'chat');
    const instanceName = selectedId.startsWith('sub-') ? selectedId.substring(4) : state.sessionName;
    
    send({
      type: 'edit_summary',
      instance_name: instanceName,
      content: content
    });
    
    saveSummaryBtn.innerHTML = `
      <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
        <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/>
      </svg>
      Updated!
    `;
    saveSummaryBtn.classList.remove('btn-primary');
    saveSummaryBtn.classList.add('btn-success');
    
    setTimeout(() => {
      saveSummaryBtn.innerHTML = `
        <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
          <path d="M17 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14c1.1 0 2-.9 2-2V7l-4-4zm-5 16c-1.66 0-3-1.34-3-3s1.34-3 3-3 3 1.34 3 3-1.34 3-3 3zm3-10H5V5h10v4z"/>
        </svg>
        Update Memory
      `;
      saveSummaryBtn.classList.remove('btn-success');
      saveSummaryBtn.classList.add('btn-primary');
    }, 2000);
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
  set('telem-sa-calls', formatNumber(telemetry.sub_agent_calls));
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
        return `<tr>
          <td title="${name}">${name}</td>
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
    const label = desc.model
      ? `${desc.model} T=${desc.temperature ?? '?'}`
      : c.config_fingerprint.slice(0, 8);
    return `<tr>
      <td title="${c.config_fingerprint}"><span class="telem-config-tag">${label}</span></td>
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
