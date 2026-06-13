# AgentCascade app.js - Module Dependency Map

## Visual Overview of Current Structure

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              app.js (4,180 lines)                           │
│                                                                       MONOLITH
└──────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                         GLOBAL SCOPE                                    │
    │  - state object (25+ properties)                                        │
    │  - ws (WebSocket)                                                        │
    │  - ~65 functions                                                         │
    │  - ~30 DOM references                                                    │
    └─────────────────────────────────────────────────────────────────────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        │                            │                            │
        ▼                            ▼                            ▼
┌──────────────┐           ┌──────────────────┐          ┌──────────────┐
│   Constants  │           │   ActivityBar    │◄───⭐     │ External CDN │
│   (Lines)    │           │   (Object Lit)   │   ONLY   │  Libraries   │
│  33-48       │           │  (172-325)       │  MODULE  │  (marked,    │
└──────────────┘           └──────────────────┘          │   DOMPurify, │
        │                     │                         │   highlight)  │
        │                     │                         └───────────────┘
        │                     │                                 │
        │                     │                                 │
        ▼                     ▼                                 │
┌──────────────────────────────────────────────────────────────┐
│                      CORE MODULES                            │
│                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │   State      │    │  WebSocket   │    │   Settings   │  │
│  │ Management   │    │ Communication│    │ Persistence  │  │
│  │ (130-170)    │    │ (843-945)    │    │ (607-839)    │  │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘  │
│         │                   │                   │           │
│         │                   │                   │           │
│         ▼                   ▼                   │           │
│  ┌──────────────┐    ┌──────────────┐          │           │
│  │   Agent      │    │handleServer  │◄─────────┘           │
│  │  Identity    │    │  Message()   │                      │
│  │  Functions   │    │ (949-1445)   │                      │
│  └──────────────┘    └──────┬───────┘                      │
│                             │                              │
│                             ▼                              │
│              ┌──────────────────────┐                      │
│              │   Message Handlers   │                      │
│              │  - state/done        │                      │
│              │  - stream_update     │                      │
│              │  - activity_update   │                      │
│              └──────────┬───────────┘                      │
│                         │                                  │
└─────────────────────────┼──────────────────────────────────┘
                          │
         ┌────────────────┼────────────────┐
         │                │                │
         ▼                ▼                ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│   Render     │  │   Actions    │  │    UI        │
│   Engine     │  │   & Modules  │  │   Components │
│              │  │              │  │              │
│ - Messages   │  │ - AFK        │  │ - Controls   │
│ - Markdown   │  │ - Sessions   │  │ - Telemetry  │
│ - Tools      │  │ - Approvals  │  │ - ContextBar │
│ - Agents     │  │ - Editing    │  │ - Tabs       │
└──────────────┘  └──────────────┘  └──────────────┘
         │                │                │
         │                │                │
         ▼                ▼                ▼
┌─────────────────────────────────────────────────┐
│              SHARED UTILITIES                   │
│  - escapeHtml()                                 │
│  - format*() functions                          │
│  - estimateTokens()                             │
│  - File processing                              │
│  - Audio (playSound)                            │
└─────────────────────────────────────────────────┘
```

---

## Module Size Distribution

```
Lines per Section:

State Management        ████████████                           ~170 lines (4%)
ActivityBar ⭐          ████████████████████                   ~154 lines (4%)
Settings Persistence    ████████████████████████████████       ~232 lines (6%)
WebSocket Comm          ██████████████                         ~150 lines (4%)
handleServerMessage()   ██████████████████████████████████████ ~496 lines (12%)
AFK System              ███                                    ~40 lines (1%)
Render Helpers          █████                                    ~48 lines (1%)
Message Rendering       █████████████████████████████████      ~488 lines (12%)
Message Editing         █████████████████████████              ~225 lines (5%)
Approval System         ███████████████████                    ~140 lines (3%)
Agent Panel Render      ████████████████████████████████       ~320 lines (8%)
Tab Switching           ███████████                            ~52 lines (1%)
Agent Selection         █████████████████                      ~112 lines (3%)
UI Controls & Stats     ████████████████████████████████       ~321 lines (8%)
Utility Functions       █████████████████████████              ~175 lines (4%)
User Actions            ████████████████████████████           ~240 lines (6%)
Telemetry & API Router  ██████████████████████████████████████ ~481 lines (12%)
Initialization          ████████████                           ~80 lines (2%)
DOM References          ████████████                           ~57 lines (1%)
Session Management      ████████████                           ~80 lines (2%)
Audio                   ████                                   ~40 lines (1%)
System Toast            ████                                   ~40 lines (1%)

TOTAL: 4,180 lines
```

---

## Dependency Heat Map

Functions with MOST dependencies (call other functions frequently):

```
🔥 handleServerMessage()     → calls 15+ functions
🔥 renderSubAgents()         → calls 10+ functions
🔥 createMessageEl()         → calls 8+ functions
🔥 loadSettings()            → calls 5+ functions
🔥 updateGenStats()          → calls 6+ functions
🔥 renderApprovals()         → calls 4+ functions

Functions with FEWEST dependencies (good extraction candidates):

✅ escapeHtml()              → 0 dependencies (pure function)
✅ formatTokenCount()        → 1 dependency
✅ estimateTokens()          → 0 dependencies (pure function)
✅ playSound()               → 2 dependencies (DOM, state)
✅ getAgentTabId()           → 0 dependencies (pure function)
✅ isSessionPrimaryAgent()   → 1 dependency (state)
```

---

## Circular Dependencies Detected

```
⚠️  renderSubAgents() ↔ switchMainTab()
    - renderSubAgents calls switchMainTab
    - switchMainTab calls updateAllContextBars which may trigger re-render
    
⚠️  handleServerMessage() ↔ renderSubAgents()
    - handleServerMessage calls renderSubAgents on 'state'/'stream_update'
    - renderSubAgents may send WebSocket messages (indirect cycle)
    
⚠️  saveSettings() ↔ getGenerateCfg()
    - saveSettings calls getGenerateCfg
    - getGenerateCfg reads from DOM that saveSettings updates
```

---

## Refactoring Priority Matrix

```
HIGH PRIORITY (High Impact, Low Risk):
┌─────────────────────────────────────────────────────┐
│ 1. Extract ActivityBar to ui/activity-bar.js        │
│    - Already modular, just file extraction          │
│    - 154 lines removed from global scope            │
├─────────────────────────────────────────────────────┤
│ 2. Extract utility functions to utils/              │
│    - Pure functions, no dependencies                │
│    - ~200 lines of testable code                    │
├─────────────────────────────────────────────────────┤
│ 3. Split handleServerMessage by type                │
│    - Reduces 496-line function to manageable chunks │
│    - Clear separation of concerns                   │
└─────────────────────────────────────────────────────┘

MEDIUM PRIORITY (Medium Impact, Medium Risk):
┌─────────────────────────────────────────────────────┐
│ 4. Extract message rendering engine                 │
│    - Complex but self-contained                     │
│    - Can be tested with mock state                  │
├─────────────────────────────────────────────────────┤
│ 5. Move settings persistence                        │
│    - Frequently accessed, good isolation candidate  │
│    - Requires careful DOM ref handling              │
├─────────────────────────────────────────────────────┤
│ 6. Extract telemetry & API router                   │
│    - Large section (481 lines)                      │
│    - Relatively independent                         │
└─────────────────────────────────────────────────────┘

LOWER PRIORITY (Lower Impact or Higher Risk):
┌─────────────────────────────────────────────────────┐
│ 7. State management module                          │
│    - Core dependency, changes affect everything     │
│    - Do after other modules are extracted           │
├─────────────────────────────────────────────────────┤
│ 8. WebSocket communication layer                    │
│    - Tightly coupled to message handlers            │
│    - Extract after handlers are split               │
├─────────────────────────────────────────────────────┤
│ 9. Full ES6 module conversion                       │
│    - Requires build tool setup                      │
│    - Do incrementally as modules are extracted      │
└─────────────────────────────────────────────────────┘
```

---

## File Size After Refactoring (Projected)

```
Current:
┌────────────────────────────────┐
│ app.js: 4,180 lines            │
│ (single monolithic file)       │
└────────────────────────────────┘

After Phase 1 (Extraction):
┌────────────────────────────────┐
│ app.js: ~500 lines             │ ← Entry + orchestration only
│ src/state/index.js: ~200       │
│ src/websocket/*.js: ~600       │
│ src/render/*.js: ~900          │
│ src/ui/*.js: ~800              │
│ src/settings/*.js: ~250        │
│ src/actions/*.js: ~300         │
│ src/utils/*.js: ~250           │
│ src/modules/*.js: ~200         │
│ TOTAL: ~4,000 lines            │ ← Same code, better organized
└────────────────────────────────┘

Benefits:
✅ Parallel development (multiple files)
✅ Easier code review (smaller files)
✅ Better test coverage (isolated modules)
✅ Reduced merge conflicts
✅ Clear API boundaries
```

---

## Build Tool Recommendations

For ES6 module support:

### Option A: Vite (Recommended)
- Fast HMR (Hot Module Replacement)
- Zero config for simple projects
- Great dev server
```bash
npm create vite@latest web_ui -- --template vanilla
```

### Option B: Webpack
- Industry standard
- More configuration options
- Larger bundle optimization
```bash
npm install --save-dev webpack webpack-cli webpack-dev-server
```

### Option C: Native ES6 Modules (Simplest)
- Modern browsers support native modules
- No build step needed initially
- Use `<script type="module">` in HTML
```html
<script type="module" src="./src/index.js"></script>
```

---

## Testing Strategy

### Unit Tests (Jest)
```
tests/
├── utils/
│   ├── escapeHtml.test.js
│   ├── formatTokenCount.test.js
│   └── estimateTokens.test.js
├── state/
│   └── agents.test.js
└── render/
    └── markdown.test.js
```

### Integration Tests (Cypress)
```
tests/integration/
├── websocket.spec.js
├── messaging.spec.js
└── settings.spec.js
```

---

*Dependency map created for refactoring planning*  
*Use with REFACTORING_ANALYSIS.md and lessons_agentcascade_refactor.md*