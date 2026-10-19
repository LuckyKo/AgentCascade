"""Regression tests for BUG_0077: model-side tool-call argument defects.

Two independent concerns, kept separate:

A. The parallel-call machinery (oai.TextChatAtOAI._chat_stream) is EXONERATED — it
   never adds or removes bytes. Tests replay recorded argument strings as a
   fragmented two-call delta stream and assert byte-exact delivery, plus a
   single-call control case. These pass before and after the fix; they are the
   invariant guard, not the bug.

B. The model-side defects are made loud and recoverable:
   - base.BaseTool._verify_json_format_args now names the keys that WERE received
     when a required key is missing (self-diagnosing error).
   - write_file coerces a payload sent under a sibling tool's key name
     (new_content / text / replacement / body) onto `content` and logs the notice.

The recorded argument strings are the verbatim bytes from the field logs
(researcher_tts-research_20261016_105818.jsonl lines 355/356 and
researcher_media-gen-refactor-plan_20261019_040921.jsonl lines 338/339). They are
embedded verbatim so the byte-exact assertions are self-contained and do not read
the live log directory.

The mock SSE plumbing is copied from tests/llm/test_oai_parallel_tool_calls.py
(that file is left untouched).
"""

from unittest.mock import patch
import json

from agent_cascade.llm.oai import TextChatAtOAI
from agent_cascade.llm.schema import Message
import agent_cascade.tools.custom.file_ops as _file_ops
from agent_cascade.tools.custom.file_ops import WriteFile


# ---------------------------------------------------------------------------
# Recorded verbatim argument strings (byte-exact; provenance in module docstring)
# ---------------------------------------------------------------------------

# Occurrence 1, call 1 (line 355): full 3-key write_file args.
FULL_ARGS_3KEY = '{"content":"---\\ntags: [media-gen, tts, koboldcpp, subprocess-lifecycle, vram]\\naliases: [kobold-tts-http-server-lifecycle, kcpp-tts-server-probe]\\nrelated: [[koboldcpp-oneshot-tts-probe]], [[media-gen-vram-seam-and-tts-options]], [[audio-cpp-cannot-load-llamacpp-tts-gguf]]\\nconfidence: verified\\nauthor: researcher_tts-research_20261016_105818.jsonl\\n---\\n\\n# koboldcpp v1.123 TTS = persistent HTTP server; one-shot `-p` is dead; PIPE capture deadlocks the child\\n\\nVerified 2026-10-16 against `N:\\\\work\\\\stuff\\\\Beta\\\\koboldcpp.exe` (v1.123). Supersedes the one-shot\\nassumption in [[koboldcpp-oneshot-tts-probe]] for pure-TTS models.\\n\\n## Working invocation (server, not one-shot)\\n`koboldcpp.exe --ttsmodel <gguf> [--ttswavtokenizer <gguf>] --ttsgpu --port <N> --quiet` launched\\n**in background**; it loads the model, then binds an HTTP server. One-shot `-p \\"<text>\\"` does NOT\\nemit WAV to stdout \\u2014 it starts the same server and waits for HTTP.\\n\\n## Readiness probe (strongest signal)\\n`GET /api/extra/version` \\u2192 200 JSON with capability flags. `tts:true` only after the TTS model is\\nloaded; with `--nomodel` it returns `tts:false` while the server is already up. So the port binds\\n**after** model load, and `tts:true` is the correct readiness gate (a bare 200 is not sufficient).\\nMeasured: Kokoro (CPU) ready in **15.3 s**; OuteTTS+WavTokenizer (2\\u00d7 RTX 5060 Ti) ready in ~60\\u201390 s.\\nStartup banner also prints `Please connect to custom endpoint at http://localhost:<port>` \\u2014 log-parsing\\nis a weaker alternative; the version endpoint is preferred.\\n\\n## \\u26a0 PIPE capture DEADLOCKS the child (confirmed by experiment)\\n`subprocess.Popen(argv, stdout=PIPE, stderr=STDOUT)` with nothing draining the pipe: the chatty\\nloader fills the ~64 KB pipe buffer, the child **blocks on write and never binds the port** \\u2014 the\\nreadiness loop never sees a server (ran 145 s, zero output, had to be killed).\\nRedirecting child stdout to a **file** made the identical script finish in **19.6 s** (ready at 15.3 s).\\n\\u21d2 For a server-spawning backend: child logs MUST go to a file (or be actively drained), never a\\nbare PIPE. This is the #1 lifecycle trap.\\n\\n## Teardown semantics on Windows\\n`proc.kill()` \\u2192 child exits **immediately (0.0 s)** with **exit code 1**. So:\\n- kill+wait is cheap and reliable \\u2014 but **never judge a killed server by its exit code** (rc==1 is\\n  normal teardown, not a failure). The current `run()` raising on non-zero exit is wrong for this shape.\\n\\n## `model` and `voice` are NOT validated by the server\\nFour POSTs to `/v1/audio/speech` on a **kokoro** server all returned 200 with **byte-identical**\\n70844-byte WAV: `voice:\\"af_bella\\"`, voice omitted, `model:\\"outetts\\"` (wrong engine), `voice:\\"zz_nope\\"`.\\n\\u21d2 The engine is chosen by the **loaded GGUF**, not the request\'s `model` field; an unknown `voice`\\nsilently falls back to the engine default. A wrong voice produces *valid but wrong* audio \\u2014 no error.\\n(OuteTTS differs: omitted voice \\u2192 random speaker, so lengths varied 88684 vs 71404 in that probe.)\\n\\n## Endpoints (from the live embedded OpenAPI spec at `/api`)\\n- `POST /v1/audio/speech` \\u2014 `OpenAISpeechRequest` = `{model, input, voice, response_format: wav|mp3 (default wav), seed, language, instruction}`, **only `input` required**. 200 \\u2192 `audio/wav` or `audio/mpeg` binary; 401 when `--password` set.\\n- `GET /v1/audio/voices` \\u2014 password-free, returns `{status, voices:[...]}`.\\n- Native `POST /api/extra/tts` takes `input, voice, speaker_json, instruction` and **does** enforce `--password`.\\n- Flags confirmed present: `--port --host --quiet --reqtimeout --nomodel --foreground --skiplauncher`. **Absent**: `--notextgen`, `--noapi`, `--ttsvoice`.\\n\\nFeeds: `N:\\\\work\\\\WD\\\\AgentCascade\\\\reports\\\\media_gen_kobold_tts_http_rewrite_SPEC.md`.\\n","justification":"Records the verified v1.123 server lifecycle facts (readiness gate, PIPE deadlock, kill semantics, voice/model non-validation) that the spec and the coder both depend on.","path":"N:\\\\work\\\\WD\\\\AgentCascade\\\\.agent_lessons\\\\koboldcpp-tts-http-server-lifecycle.md"}'

# Occurrence 1, call 2 (line 356): same completion, `path` key absent from the
# model's emitted JSON (the model-side omission).
MISSING_PATH_ARGS = '{"content":"---\\ntags: [audio-cpp, tts, gguf-compat, backend-alternative]\\naliases: [audio-cpp-gguf-incompat, audiocpp-not-dropin-for-kobold-models]\\nrelated: [[koboldcpp-oneshot-tts-probe]], [[koboldcpp-tts-http-server-lifecycle]]\\nconfidence: verified\\nauthor: researcher_tts-research_20261016_105818.jsonl\\n---\\n\\n# audio.cpp is NOT a drop-in replacement for the llama.cpp-style TTS GGUFs we already have\\n\\nEvaluated 2026-10-16 as an alternative to koboldcpp for the `kobold_tts` backend.\\n\\n## What is true locally\\n`N:\\\\work\\\\WD\\\\audio.cpp` (branch `dev`, repo copy dated 2026-09-15) has a **built** binary:\\n`build\\\\windows-cuda-release\\\\bin\\\\audiocpp_cli.exe` (74.5 MB, CUDA sm_120). It works \\u2014 it was used\\nfor Yue2 music gen (see that repo\'s own `.agent_lessons/windows-cuda-build-setup.md`).\\n\\nIts CLI is a **true one-shot**: `--task tts --family <fam> --model <path> --backend cuda --text \\"...\\" --out out.wav`\\n\\u2192 writes a WAV file and exits. No server, no port, no lingering GPU process. Architecturally this is\\na *simpler* shape than the koboldcpp HTTP-server rewrite.\\n\\n## Why it is not usable with our current models (proof: direct `--inspect` runs)\\nOur files are llama.cpp-style GGUFs; audio.cpp expects its **own** package format.\\n- `--inspect --family kokoro_tts --model N:\\\\work\\\\stuff\\\\Beta\\\\audio\\\\Kokoro_no_espeak_Q4.gguf` \\u2192\\n  `gguf_init_from_file_ptr: tensor name 406 is too long: 64 >= 64` \\u2192 `failed to read GGUF metadata`.\\n  Hard format incompatibility (its GGUF reader rejects the llama.cpp Kokoro layout).\\n- `--inspect --family outetts --model OuteTTS-0.3-1B-Q4_0.gguf` \\u2192\\n  `missing model package file \'config\': ...\\\\config.json` \\u2192 audio.cpp\'s `outetts` is a **multi-file\\n  directory package**, not a single GGUF.\\n- audio.cpp\'s own packages (`models/Kokoro-82M-GGUF/kokoro-82m-q8_0.gguf`,\\n  `models/Llama-OuteTTS-1.0-1B_Q8/...`) are installed via `tools/model_manager_v2.py install ...`\\n  and are **not present locally** (no `models/` dir in the repo).\\n- Voice naming differs: audio.cpp uses `--voice-id` / `--voice-ref` (Kokoro 82M, \\"54 preset voices\\"),\\n  not kobold\'s `af_bella`.\\n\\n## Decision guidance\\nSwitching to audio.cpp = a **model-download decision**, not a code change: new GGUF packages, new\\nconfig surface, new voice vocabulary. It would however let the backend keep the original one-shot\\nsubprocess design (no server lifecycle, no port allocation, no teardown risk, no PIPE deadlock).\\nPresent it as an option; do not treat it as a drop-in.\\n","justification":"Records the audio.cpp alternative with its proof method so the next agent does not re-investigate or mistake it for a drop-in."}'

# Occurrence 2 (line 338): the model used edit_file's key `new_content` instead of
# `content` (the renamed-key variant).
RENAMED_KEY_ARGS = '{"justification":"File the currently-open data-loss bug found during the media_gen refactor planning: a UI save silently deletes backend config blocks it does not render (chatterbox currently unusable)","new_content":"# BUG_0093 — POST /api/media_gen is a full-document replace: a UI save silently deletes every backend config block it does not render\\n\\n**Status:** OPEN\\n**Filed:** 2026-10-19\\n**Found in:** DIG-phase planning for media_gen modularization (`reports/media_gen-refactor-impl-plan.md`), confirmed by `git diff -- config/media_gen/media_gen.json` and by reading the save path; independently re-verified by a reviewer agent @ `AgentCascade` @ `e7375d4801222a25fcc9194c13f9742d5201a89c`\\n**Severity:** high (silent config data loss; a shipped, registered, tested backend is unusable in this deployment)\\n**Component:** `agent_cascade/api_server.py:1885-1930` (`save_media_gen_config`), `web_ui/app.js:7195-7283` (`saveMediaGenSettings`), `config/media_gen/media_gen.json`\\n\\n## Symptom\\n`config/media_gen/media_gen.json` in the working tree no longer contains the `chatterbox` block that commit `c900064b` added. Consequences, all observable:\\n- `media_gen` with `backend=chatterbox` fails at preflight: `No Chatterbox interpreter configured. Set chatterbox.python in media_gen.json`.\\n- `media_gen prompt=\\"__help\\"` does not list `chatterbox` at all (`_format_media_help` skips a missing/non-dict block, `agent_cascade/tools/media_gen.py:472-474`), so the agent cannot discover the backend either.\\n- The API returns `{\\"status\\":\\"ok\\"}` for the save that destroyed the block; the file now ends with no trailing newline (the `json.dump` signature).\\n- Same mechanism also threatens keys with no UI control at all (`kobold_tts.ready_timeout`, `kobold_tts.port_range`).\\n\\n## Evidence\\n- `git diff -- config/media_gen/media_gen.json` shows the whole `chatterbox` block removed vs HEAD; `git show HEAD:config/media_gen/media_gen.json` still contains it.\\n- Write path is a verbatim write of the posted body: `json.dump(data, f, indent=2)` at `api_server.py:1923-1924`. The only merge logic is the narrow `media_types` re-attach loop at `:1917-1922`, which only fires for keys already present in the body — a partial fixup, not a merge.\\n- The UI builds the body from three hardcoded blocks only: `body.comfyui` (`app.js:7224-7231`), `body.sdcpp` (`:7241-7247`), `body.kobold_tts` (`:7257-7263`); `index.html:866-870` has exactly three `<option>`s and no chatterbox block.\\n- No test asserts block preservation. `tests/test_api_endpoints.py::TestMediaGenProjectRoot::test_project_root_is_never_persisted` (`:2473-2484`) POSTs `{\'timeout\': 180, \'project_root\': \'C:/evil\'}` with **no** backend blocks, so mid-test the on-disk file really is reduced to `{\\"timeout\\": 180}` — harmless only because the `_isolate_config` fixture (`:2417-2448`) restores the file. Reviewer-verified.\\n- Load-bearing for an existing test: `tests/tools/test_media_gen_video.py:315` (`test_preflight_requires_output_dir`) proves comfyui `preflight` raises `\'No comfyui.output_dir configured\'` when that key is empty — i.e. a UI save that drops comfyui\'s unrendered keys breaks the video path too.\\n\\n## Root cause\\nDiagnosed, two independent halves:\\n1. **Backend:** the save handler writes the request body as the new document; there is no read-back-and-update step. Any top-level key the client does not send is destroyed.\\n2. **Frontend:** the save handler constructs the body from a hardcoded list of form fields for three backends, so the fourth registered backend is never sent.\\nThe validation tuple at `api_server.py:1902` (`(\'comfyui\',\'sdcpp\',\'kobold_tts\')`) is **validation only** — persistence is generic — so the missing `chatterbox` entry there is a consistency defect, not the cause of the loss.\\n\\n## Fix\\nProposed (not yet implemented; full detail in `reports/media_gen-refactor-impl-plan.md` W4/W5):\\n- **Merge-preserve (backend, the durable fix):** replace the `media_types`-only loop at `api_server.py:1917-1922` with two levels — (a) `data.setdefault(block_key, old_block)` for any block present on disk but absent from the body, and (b) inside each posted block, `block_val.setdefault(k, v)` for keys the UI does not render. Note the accepted limitation: a key can no longer be *deleted* from the UI (the UI writes `\'\'` for cleared fields, so clearing still persists).\\n- **Render-all (frontend):** render every registered backend\'s fields from a single manifest so the body always covers all blocks. Needed anyway for the chatterbox controls to exist.\\n- Do both: merge-preserve is the safety net for future backends; render-all is required for the new controls.\\n\\n## Verification\\nNot yet fixed. Required before moving to `fixed/`:\\n- New regression test in `tests/test_api_endpoints.py::TestMediaGenProjectRoot`: seed a 4-block file, POST a body containing only `comfyui` + `timeout`, assert `chatterbox`/`sdcpp`/`kobold_tts` blocks survive intact on disk **and** that unrendered keys inside the posted block (`media_types`, `output_dir`, `default_workflow`) survive; plus a case with an unknown future block key.\\n- Manual: restore the `chatterbox` block, click Save in the UI, re-read `config/media_gen/media_gen.json`, confirm all four blocks survive and `backend=chatterbox` generates.\\n\\n## Related\\n[[media-gen-ui-save-drops-unknown-backend-blocks]] (root-cause memory), [[media-gen-add-backend-touchpoints]], [[media-gen-vram-dance-capability-gated-requires-vram-unused]], [[settings-save-merge-vs-replace-audit]] (the reusable audit procedure), `reports/media_gen-refactor-impl-plan.md` §W4/§W5/§W6\\n","path":"N:\\\\work\\\\WD\\\\AgentCascade\\\\.bug_tracker\\\\BUG_0093_media_gen_post_replace_deletes_unrendered_backend_blocks.md"}'


# ---------------------------------------------------------------------------
# Mock SSE plumbing (copied from test_oai_parallel_tool_calls.py)
# ---------------------------------------------------------------------------


class _Fn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, id=None, function=None):
        self.id = id
        self.function = function


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        if tool_calls is not None:
            self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, choices, model=None, usage=None):
        self.choices = choices
        if model is not None:
            self.model = model
        if usage is not None:
            self.usage = usage


class _Resp:
    def __init__(self, chunks):
        self._events = []
        for c in chunks:
            ch = c.choices[0]
            delta = {}
            if getattr(ch.delta, 'content', None) is not None:
                delta['content'] = ch.delta.content
            if getattr(ch.delta, 'tool_calls', None) is not None:
                delta['tool_calls'] = [{
                    'id': tc.id,
                    'function': {
                        'name': tc.function.name,
                        'arguments': tc.function.arguments,
                    },
                } for tc in ch.delta.tool_calls]
            self._events.append(_SSE('chunk', {'choices': [{'delta': delta, 'finish_reason': ch.finish_reason}]}))
        self._client = type('_Client', (), {
            '_process_response_data': staticmethod(lambda data, cast_to=None, response=None: _from_dict(data))
        })()
        self._cast_to = None
        self.response = None

    def _iter_events(self):
        return iter(self._events)

    def close(self):
        pass


class _SSE:
    def __init__(self, data, payload):
        self.data = data
        self._payload = payload

    def json(self):
        return self._payload


def _from_dict(payload):
    choice = payload['choices'][0]
    d = choice.get('delta', {})
    tool_calls = None
    if d.get('tool_calls') is not None:
        tool_calls = [_TC(id=tc.get('id'), function=_Fn(name=(tc.get('function') or {}).get('name'),
                                                        arguments=(tc.get('function') or {}).get('arguments')))
                      for tc in d['tool_calls']]
    delta = _Delta(content=d.get('content'), tool_calls=tool_calls)
    return _Chunk(choices=[_Choice(delta, finish_reason=choice.get('finish_reason'))])


def _make_client():
    return TextChatAtOAI({'api_base': 'http://127.0.0.1:9/v1', 'model': 'my-alias'})


def _run_stream(llm, chunks):
    """Drive the REAL _chat_stream with mocked SSE; return the final yielded message list."""
    llm._chat_complete_create = lambda **kwargs: _Resp(chunks)
    with patch('agent_cascade.utils.streaming.watch_stream', side_effect=lambda it, *a, **k: iter(it)):
        frames = list(llm.chat(messages=[Message('user', 'do two things')], stream=True, delta_stream=False))
    return frames[-1] if frames else []


def _fragment(s, n=40):
    """Split an args string into n-char deltas (mirrors the field's fragmented SSE stream)."""
    return [s[i:i + n] for i in range(0, len(s), n)] or ['']


def _two_call_stream(id_a, id_b, args_a, args_b, drop_a=None, n=40):
    """Build a fragmented two-call delta stream.

    Both calls open with their id + name on the first chunk; continuation deltas
    carry the id (no name) and the next fragment. `drop_a` optionally omits one
    continuation fragment of call A to simulate a genuine transport fault.
    """
    fa = _fragment(args_a, n)
    fb = _fragment(args_b, n)
    if drop_a is not None:
        fa = fa[:drop_a] + fa[drop_a + 1:]
    chunks = []
    chunks.append(_Chunk(choices=[_Choice(_Delta(tool_calls=[
        _TC(id=id_a, function=_Fn(name='write_file', arguments=fa[0])),
        _TC(id=id_b, function=_Fn(name='write_file', arguments=fb[0])),
    ]))]))
    for i in range(1, max(len(fa), len(fb))):
        tcs = []
        if i < len(fa):
            tcs.append(_TC(id=id_a, function=_Fn(arguments=fa[i])))
        if i < len(fb):
            tcs.append(_TC(id=id_b, function=_Fn(arguments=fb[i])))
        if tcs:
            chunks.append(_Chunk(choices=[_Choice(_Delta(tool_calls=tcs))]))
    return chunks


def _delivered_by_id(msgs):
    """Map function_id -> delivered arguments string."""
    out = {}
    for m in msgs:
        if m.function_call is not None:
            out[m.extra.get('function_id')] = m.function_call.arguments
    return out


# ---------------------------------------------------------------------------
# Isolated operation-manager double for the real-tool tests (no real file I/O)
# ---------------------------------------------------------------------------


class _RecordingOM:
    """Captures the exact args forwarded to the operation manager (isolated workspace)."""

    def __init__(self):
        self.write = None

    def write_file(self, path, content, agent_name=None, justification=''):
        self.write = (path, content)
        return 'written'


class _FakePool:
    def __init__(self, om):
        self.operation_manager = om


def _write_file_tool(om):
    return WriteFile(agent_pool=_FakePool(om), agent_name='tester')


# ---------------------------------------------------------------------------
# 1. Recorded parallel stream is delivered byte-exact (the invariant guard)
# ---------------------------------------------------------------------------


def test_recorded_parallel_stream_is_delivered_byte_exact():
    """Replay the recorded two-call args as a fragmented stream; assert byte-exact delivery."""
    llm = _make_client()
    chunks = _two_call_stream('id_A', 'id_B', FULL_ARGS_3KEY, MISSING_PATH_ARGS)
    msgs = _run_stream(llm, chunks)
    tool_msgs = [m for m in msgs if m.function_call is not None]
    assert len(tool_msgs) == 2, f"expected 2 function_call messages, got {len(tool_msgs)}"
    got = _delivered_by_id(msgs)
    assert got['id_A'] == FULL_ARGS_3KEY, 'call A bytes changed'
    assert got['id_B'] == MISSING_PATH_ARGS, 'call B bytes changed'


# ---------------------------------------------------------------------------
# 2. The self-diagnosing required-param error names the received keys
#    (the bug test: FAILS before the base.py change, passes after)
# ---------------------------------------------------------------------------


def test_missing_required_error_names_received_keys():
    """Feed the recorded 2-key args through the REAL WriteFile; the error must name
    the keys that were received, while keeping the legacy prefix and quoted key."""
    om = _RecordingOM()
    t = _write_file_tool(om)
    out = t.call(MISSING_PATH_ARGS)
    assert out.startswith('ERROR')
    # Legacy contract (must remain byte-identical so the pinned tests cannot break).
    assert "missing required parameter(s) for tool 'write_file': ['path']" in out
    assert "'path'" in out
    # New self-diagnosing detail: names exactly what was received and what is allowed.
    assert "received keys: ['content', 'justification']" in out
    assert "required: ['path', 'content']" in out
    assert "known parameters: ['path', 'content', 'justification']" in out
    assert "re-issue with the exact key 'path'." in out


# ---------------------------------------------------------------------------
# 3. A stream fault has a DIFFERENT signature than a model omission
# ---------------------------------------------------------------------------


def test_stream_fault_signature_differs_from_model_omission():
    """A genuinely dropped mid-JSON fragment splices the tail into the previous
    value: valid JSON with a garbled value and ALL keys present. That is not the
    shape of the recorded model omission (a clean object with a required key absent)."""
    clean = '{"content":"hello world file","justification":"see notes"}'
    llm = _make_client()
    # n=9 fragments so drop_a=2 removes a chunk from inside the first value; the
    # surviving tail splices into it -> valid JSON, all keys present, garbled value.
    chunks = _two_call_stream('id_A', 'id_B', clean, '{"x":1}', drop_a=2, n=9)
    msgs = _run_stream(llm, chunks)
    got = _delivered_by_id(msgs)
    spliced = json.loads(got['id_A'])          # still valid JSON
    assert set(spliced) == {'content', 'justification'}, 'a real fault keeps all keys'
    assert spliced['content'] != 'hello world file', 'the surviving value must be garbled (spliced)'

    # The recorded omission is the opposite signature: a required key is simply absent.
    parsed_omission = json.loads(MISSING_PATH_ARGS)
    assert 'path' not in parsed_omission, 'model omission drops a key, not a value'


# ---------------------------------------------------------------------------
# 4. write_file coerces a renamed payload key (the model's edit_file habit)
# ---------------------------------------------------------------------------


def test_write_file_renamed_key_is_coerced():
    """Feed the recorded occurrence-2 args (justification/new_content/path) into
    WriteFile: the file must be written and a coercion notice logged."""
    expected = json.loads(RENAMED_KEY_ARGS)
    om = _RecordingOM()
    t = _write_file_tool(om)
    with patch.object(_file_ops, 'logger') as lg:
        t.call(RENAMED_KEY_ARGS)
    assert om.write is not None, 'coerced call did not reach the filesystem'
    assert om.write[0] == expected['path'], 'wrong path forwarded'
    assert om.write[1] == expected['new_content'], 'content not coerced from new_content'
    notice = ' '.join(str(c) for c in lg.info.call_args_list)
    assert 'coerced alias' in notice, 'no coercion notice logged'
    assert "'new_content'" in notice, 'coercion notice does not name the alias'


# ---------------------------------------------------------------------------
# 5. Single-call control case (must stay byte-exact)
# ---------------------------------------------------------------------------


def test_single_call_fragmented_is_delivered_byte_exact():
    """One fragmented write_file call is delivered byte-exact (no single-call regression)."""
    llm = _make_client()
    fa = _fragment(FULL_ARGS_3KEY)
    chunks = [_Chunk(choices=[_Choice(_Delta(tool_calls=[
        _TC(id='id_A', function=_Fn(name='write_file', arguments=fa[0])),
    ]))])]
    for i in range(1, len(fa)):
        chunks.append(_Chunk(choices=[_Choice(_Delta(tool_calls=[
            _TC(id='id_A', function=_Fn(arguments=fa[i])),
        ]))]))
    msgs = _run_stream(llm, chunks)
    tool_msgs = [m for m in msgs if m.function_call is not None]
    assert len(tool_msgs) == 1, f"single call must stay one entry, got {len(tool_msgs)}"
    assert tool_msgs[0].function_call.arguments == FULL_ARGS_3KEY, 'single-call bytes changed'
