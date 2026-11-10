"""Phase-1 omni-multimodal schema tests (M1) + Phase-2 adapter-gate invariant.

Covers:
  1. ``APIEndpoint`` per-modality capability flags (M1):
       * ``audio_enabled`` / ``video_enabled`` default False, so every existing
         endpoint/config behaves exactly as today;
       * ``from_dict`` round-trips them (default False when absent);
       * ``to_dict`` (asdict) and ``to_llm_cfg`` both carry them to the adapter cfg.
  2. Adapter gate invariant (Phase 2): a ``qwenvl_oai`` endpoint emits the
     ``input_audio`` / ``video_url`` media parts ONLY when the runtime-merged
     generate_cfg sets ``audio_enabled`` / ``video_enabled``; with the flags
     absent/False it keeps audio/video → TEXT (``[Audio file: …]`` /
     ``[Video file: …]``), byte-for-byte today's behavior.

All tests are self-contained (no LLM / HTTP / network).
"""

import base64
import shutil

import pytest

from agent_cascade.api_router_pkg.endpoints import APIEndpoint

# Transcode tests need the system ffmpeg the omni-audio normalizer shells; the
# pass-through tests never do. Skip the transcode tests on ffmpeg-less boxes so the
# suite stays green there (ffmpeg IS present on the omni-audio box and runs them).
_HAS_FFMPEG = shutil.which('ffmpeg') is not None


class TestModalityFlags:

    def test_defaults_are_false(self):
        """New flags default False so existing endpoints are unchanged."""
        ep = APIEndpoint(name='x', api_base='http://x/v1', model='m')
        assert ep.vision_enabled is True
        assert ep.audio_enabled is False
        assert ep.video_enabled is False

    def test_from_dict_defaults_when_absent(self):
        """An existing config dict with NO audio/video keys loads unchanged."""
        ep = APIEndpoint.from_dict({'name': 'x', 'api_base': 'http://x/v1', 'model': 'm'})
        assert ep.vision_enabled is True
        assert ep.audio_enabled is False
        assert ep.video_enabled is False

    def test_from_dict_round_trip(self):
        """Round-trip preserves explicitly-set audio/video values."""
        data = {
            'id': 'ep_x',
            'name': 'x',
            'api_base': 'http://x/v1',
            'model': 'm',
            'vision_enabled': True,
            'audio_enabled': True,
            'video_enabled': True,
        }
        ep = APIEndpoint.from_dict(data)
        assert ep.audio_enabled is True
        assert ep.video_enabled is True
        out = ep.to_dict()
        assert out['audio_enabled'] is True
        assert out['video_enabled'] is True

    def test_to_llm_cfg_emits_flags(self):
        """to_llm_cfg carries the flags through to the adapter cfg dict."""
        ep = APIEndpoint(name='x', api_base='http://x/v1', model='m',
                         audio_enabled=True, video_enabled=False)
        cfg = ep.to_llm_cfg()
        assert cfg['audio_enabled'] is True
        assert cfg['video_enabled'] is False
        assert cfg['vision_enabled'] is True


class TestAdapterGateInvariant:
    """Phase-2 gate invariant for the shared ``qwenvl_oai`` adapter.

    The adapter opens its audio/video emission gates ONLY when the runtime-merged
    generate_cfg sets ``audio_enabled`` / ``video_enabled``. At runtime those
    endpoint flags ride in ``_build_merged_cfg`` → ``llm.chat(extra_generate_cfg=…)``
    and are threaded into ``convert_messages_to_dicts(generate_cfg=…)`` — exactly
    mirroring how ``effective_vision`` in ``llm/base.py`` reads ``vision_enabled``.
    We reproduce that here by passing ``generate_cfg=`` directly to the converter.

    The #1 safety property is the default case: with the flags absent/False the
    output is byte-for-byte today's text fallback.
    """

    @staticmethod
    def _adapter():
        from agent_cascade.llm import get_chat_model
        return get_chat_model({
            'model': 'x',
            'api_base': 'http://localhost:9/v1',
            'model_server': 'http://localhost:9/v1',
            'api_key': 'EMPTY',
            'model_type': 'qwenvl_oai',
        })

    @staticmethod
    def _messages():
        from agent_cascade.llm.schema import ContentItem, Message
        # data-URI values so ``conv_multimodel_value`` returns them unchanged (no file I/O).
        return [Message(role='user', content=[
            ContentItem(text='hi'),
            ContentItem(audio='data:;base64,AAAA'),
            ContentItem(video='data:video/mp4;base64,BBB'),
        ])]

    def _content(self, generate_cfg):
        return self._adapter().convert_messages_to_dicts(
            self._messages(), generate_cfg=generate_cfg)[0]['content']

    def test_support_audio_input_class_default_is_false(self):
        # The class capability stays False; only the endpoint cfg flag opens the gate.
        assert self._adapter().support_audio_input is False

    def test_flags_absent_keep_text_fallback(self):
        """#1 invariant: no flag in the effective cfg → text fallback (today's behavior)."""
        parts = self._content(None)  # converter falls back to the template generate_cfg
        types = [p['type'] for p in parts]
        assert 'input_audio' not in types
        assert 'video_url' not in types and 'video' not in types
        assert {'type': 'text', 'text': '[Audio file: data:;base64,AAAA]'} in parts
        assert {'type': 'text', 'text': '[Video file: data:video/mp4;base64,BBB]'} in parts

    def test_flags_false_keep_text_fallback(self):
        parts = self._content({'audio_enabled': False, 'video_enabled': False})
        assert {'type': 'text', 'text': '[Audio file: data:;base64,AAAA]'} in parts
        assert {'type': 'text', 'text': '[Video file: data:video/mp4;base64,BBB]'} in parts

    def test_audio_enabled_emits_input_audio(self):
        parts = self._content({'audio_enabled': True})
        audio = [p for p in parts if p['type'] == 'input_audio']
        assert len(audio) == 1
        # #2: the inline data-URI is stripped to pure base64; untagged → no format key.
        assert audio[0]['input_audio']['data'] == 'AAAA'
        assert 'format' not in audio[0]['input_audio']
        # Video must remain gated off independently of the audio flag.
        assert {'type': 'text', 'text': '[Video file: data:video/mp4;base64,BBB]'} in parts

    def test_video_enabled_emits_video_url(self):
        parts = self._content({'video_enabled': True})
        vid = [p for p in parts if p['type'] == 'video_url']
        assert len(vid) == 1
        assert vid[0]['video_url']['url'] == 'data:video/mp4;base64,BBB'
        # Audio must remain gated off independently of the video flag.
        assert {'type': 'text', 'text': '[Audio file: data:;base64,AAAA]'} in parts

    def test_both_flags_emit_both_media_parts(self):
        parts = self._content({'audio_enabled': True, 'video_enabled': True})
        types = [p['type'] for p in parts]
        assert 'input_audio' in types
        assert 'video_url' in types
        assert '[Audio file:' not in ' '.join(p.get('text', '') for p in parts)
        assert '[Video file:' not in ' '.join(p.get('text', '') for p in parts)

    def _content_with(self, messages, generate_cfg):
        return self._adapter().convert_messages_to_dicts(
            messages, generate_cfg=generate_cfg)[0]['content']

    @staticmethod
    def _msg(media_key, value):
        from agent_cascade.llm.schema import ContentItem, Message
        return [Message(role='user', content=[ContentItem(**{media_key: value})])]

    # --- Local-file encoding path: this is the only route that produces the
    # pure-base64+format audio shape and the MIME-tagged video data-URI. ---
    def test_audio_local_path_emits_pure_base64_with_format(self, tmp_path):
        import base64
        wav = tmp_path / 'clip.wav'
        raw = b'RIFF\x00\x00\x00WAVEfakeaudio'
        wav.write_bytes(raw)
        parts = self._content_with(self._msg('audio', str(wav)), {'audio_enabled': True})
        audio = [p for p in parts if p['type'] == 'input_audio'][0]['input_audio']
        # Pure base64 (no data: prefix) + format from the .wav extension.
        assert not audio['data'].startswith('data:')
        assert base64.b64decode(audio['data']) == raw
        assert audio['format'] == 'wav'

    def test_audio_local_path_format_follows_extension(self, tmp_path):
        mp3 = tmp_path / 'track.mp3'
        mp3.write_bytes(b'ID3fake')
        parts = self._content_with(self._msg('audio', str(mp3)), {'audio_enabled': True})
        assert [p for p in parts if p['type'] == 'input_audio'][0]['input_audio']['format'] == 'mp3'

    def test_audio_data_uri_input_keeps_no_format(self, tmp_path):
        # Inline data-URI input is left unchanged and carries NO format key (no regression).
        parts = self._content_with(self._msg('audio', 'data:;base64,AAAA'), {'audio_enabled': True})
        audio = [p for p in parts if p['type'] == 'input_audio'][0]['input_audio']
        assert 'format' not in audio

    def test_video_local_path_emits_mime_tagged_data_uri(self, tmp_path):
        mp4 = tmp_path / 'clip.mp4'
        mp4.write_bytes(b'\x00\x00\x00\x18ftypmp42')
        parts = self._content_with(self._msg('video', str(mp4)), {'video_enabled': True})
        url = [p for p in parts if p['type'] == 'video_url'][0]['video_url']['url']
        assert url.startswith('data:video/mp4;base64,')

    def test_video_local_path_mime_follows_extension(self, tmp_path):
        webm = tmp_path / 'clip.webm'
        webm.write_bytes(b'\x1a\x45\xdf\xa3fake')
        parts = self._content_with(self._msg('video', str(webm)), {'video_enabled': True})
        url = [p for p in parts if p['type'] == 'video_url'][0]['video_url']['url']
        assert url.startswith('data:video/webm;base64,')


class TestCapabilitySubclassGate:
    """#1: a capability-True subclass (``support_audio_input`` True) keeps emitting
    ``input_audio`` even when the always-present ``audio_enabled`` cfg flag is False.

    ``APIEndpoint.to_llm_cfg`` emits ``audio_enabled`` on every call (dataclass
    default False), so the key is ALWAYS present at runtime. The old gate
    ``.get('audio_enabled', self.support_audio_input)`` let that present-False win
    and silently stripped ``QwenOmniChatAtOAI`` of audio. The gate now composes the
    class capability OR the flag, so a declared capability survives a False flag.
    Uses the real registered ``qwenomni_oai`` adapter.
    """

    @staticmethod
    def _omni():
        from agent_cascade.llm import get_chat_model
        return get_chat_model({
            'model': 'x',
            'api_base': 'http://localhost:9/v1',
            'model_server': 'http://localhost:9/v1',
            'api_key': 'EMPTY',
            'model_type': 'qwenomni_oai',
        })

    @staticmethod
    def _msg(value):
        from agent_cascade.llm.schema import ContentItem, Message
        return [Message(role='user', content=[ContentItem(audio=value)])]

    def _content(self, adapter, generate_cfg):
        return adapter.convert_messages_to_dicts(
            self._msg('data:;base64,AAAA'), generate_cfg=generate_cfg)[0]['content']

    def test_capability_is_true(self):
        assert self._omni().support_audio_input is True

    def test_capability_true_keeps_audio_when_flag_false(self):
        """#1 core: capability True + cfg audio_enabled=False -> STILL input_audio."""
        parts = self._content(self._omni(), {'audio_enabled': False})
        assert [p for p in parts if p['type'] == 'input_audio'], parts
        assert '[Audio file:' not in ' '.join(p.get('text', '') for p in parts)

    def test_capability_true_keeps_audio_when_flag_absent(self):
        """Proves the OR (capability alone opens the gate), not cfg-only."""
        parts = self._content(self._omni(), None)
        assert [p for p in parts if p['type'] == 'input_audio'], parts


class TestInlineMediaDataURI:
    """#2/#5: inline data-URI normalization and the no-'format' fallback.

    ``content_parse``'s ``MediaStorageError`` fallback hands the converter a raw
    data-URI string; audio must become PURE base64 + a MIME-derived format, and an
    untagged video data-URI must be tagged ``video/mp4`` (a tagged one is kept).
    """

    @staticmethod
    def _adapter():
        from agent_cascade.llm import get_chat_model
        return get_chat_model({
            'model': 'x',
            'api_base': 'http://localhost:9/v1',
            'model_server': 'http://localhost:9/v1',
            'api_key': 'EMPTY',
            'model_type': 'qwenvl_oai',
        })

    @staticmethod
    def _msg(key, value):
        from agent_cascade.llm.schema import ContentItem, Message
        return [Message(role='user', content=[ContentItem(**{key: value})])]

    def _audio(self, value, cfg):
        parts = self._adapter().convert_messages_to_dicts(
            self._msg('audio', value), generate_cfg=cfg)[0]['content']
        return [p for p in parts if p['type'] == 'input_audio'][0]['input_audio']

    def _video(self, value, cfg):
        parts = self._adapter().convert_messages_to_dicts(
            self._msg('video', value), generate_cfg=cfg)[0]['content']
        return [p for p in parts if p['type'] == 'video_url'][0]['video_url']['url']

    # #2 audio: strip the 'data:<mime>;base64,' prefix -> pure base64 + format.
    def test_audio_data_uri_mime_strips_prefix_and_sets_format(self):
        audio = self._audio('data:audio/wav;base64,QUFB', {'audio_enabled': True})
        assert audio['data'] == 'QUFB'
        assert audio['format'] == 'wav'

    def test_audio_data_uri_mime_subtype_follows(self):
        assert self._audio('data:audio/mp3;base64,QUFB', {'audio_enabled': True})['format'] == 'mp3'

    # #2 video: untagged inline data-URI is tagged video/mp4; a tagged one kept verbatim.
    def test_video_untagged_data_uri_is_tagged_mp4(self):
        assert self._video('data:;base64,BBCC', {'video_enabled': True}) == 'data:video/mp4;base64,BBCC'

    def test_video_tagged_data_uri_kept_verbatim(self):
        assert self._video('data:video/webm;base64,BBCC', {'video_enabled': True}) == 'data:video/webm;base64,BBCC'

    # #5: an extensionless local audio path carries NO format (no longer asserts 'wav').
    def test_audio_local_path_no_extension_has_no_format(self, tmp_path):
        path = tmp_path / 'noext'
        path.write_bytes(b'\x00\x00\x00\x14ftypfake')
        audio = self._audio(str(path), {'audio_enabled': True})
        assert not audio['data'].startswith('data:')  # encoded to pure base64
        assert 'format' not in audio


class TestAudioContainerNormalization:
    """Omni audio-input container normalization.

    The LFM3-Omni / llama.cpp omni server decodes audio via MiniAudio, which only
    understands wav/mp3/flac (and sniffs the bytes, ignoring 'format'). AC accepts
    ogg/m4a/opus too, so those must be transcoded to mono MP3 at the encode boundary
    or the model silently receives no usable audio. Pass-through (decodable) tests run
    everywhere; transcode tests are ffmpeg-gated so the suite stays green on boxes
    without ffmpeg (they run on the omni-audio box, which has ffmpeg v8.0 on PATH).
    """

    @staticmethod
    def _adapter():
        from agent_cascade.llm import get_chat_model
        return get_chat_model({
            'model': 'x',
            'api_base': 'http://localhost:9/v1',
            'model_server': 'http://localhost:9/v1',
            'api_key': 'EMPTY',
            'model_type': 'qwenvl_oai',
        })

    def _emit_audio(self, value, cfg=None):
        """Convert a single audio ContentItem and return the emitted input_audio part.

        The omni gate is opened via generate_cfg only (the qwenvl_oai class capability
        is False), exactly mirroring the runtime-merged cfg path.
        """
        from agent_cascade.llm.schema import ContentItem, Message
        parts = self._adapter().convert_messages_to_dicts(
            [Message(role='user', content=[ContentItem(audio=value)])],
            generate_cfg={'audio_enabled': True} if cfg is None else cfg,
        )[0]['content']
        audio = [p for p in parts if p['type'] == 'input_audio']
        assert audio, parts
        return audio[0]['input_audio']

    @staticmethod
    def _is_mp3_bytes(b: bytes) -> bool:
        """True if ``b`` starts with an MP3 magic (ID3v2 tag or MPEG frame sync)."""
        return b[:3] == b'ID3' or (len(b) >= 2 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0)

    @staticmethod
    def _write_wav(path) -> None:
        """Write a small valid 16 kHz mono WAV so ffmpeg has a decodable source."""
        import math
        import struct
        import wave
        with wave.open(str(path), 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            for i in range(16000 * 3):  # 0.3 s of a 440 Hz tone
                w.writeframesraw(struct.pack('<h', int(2000 * math.sin(2 * math.pi * 440 * i / 16000))))

    def _real_container(self, tmp_path, ext: str):
        """Produce a real non-decodable container (ogg/m4a) from a generated WAV via ffmpeg."""
        import subprocess
        src = tmp_path / 'src.wav'
        self._write_wav(src)
        out = tmp_path / f'audio.{ext}'
        codec = 'libvorbis' if ext == 'ogg' else 'aac'
        subprocess.run(['ffmpeg', '-y', '-i', str(src), '-c:a', codec, str(out)],
                       check=True, capture_output=True)
        return out

    # --- Pass-through: decodable containers are forwarded byte-for-byte (no ffmpeg). ---
    def test_audio_local_mp3_passes_through(self, tmp_path):
        mp3 = tmp_path / 'track.mp3'
        raw = b'ID3\x03\x00\x00' + b'\x00' * 64  # ID3 magic, never decoded (pass-through)
        mp3.write_bytes(raw)
        part = self._emit_audio(str(mp3))
        assert part['format'] == 'mp3'
        assert base64.b64decode(part['data']) == raw  # unchanged, no re-encode

    def test_audio_local_wav_and_flac_pass_through(self, tmp_path):
        wav = tmp_path / 'a.wav'
        wav_raw = b'RIFF\x00\x00\x00WAVEfake'
        wav.write_bytes(wav_raw)
        part = self._emit_audio(str(wav))
        assert part['format'] == 'wav'
        assert base64.b64decode(part['data']) == wav_raw

        flac = tmp_path / 'b.flac'
        flac_raw = b'fLaC' + b'\x00' * 32
        flac.write_bytes(flac_raw)
        part = self._emit_audio(str(flac))
        assert part['format'] == 'flac'
        assert base64.b64decode(part['data']) == flac_raw

    # --- Transcode to mono MP3 (ffmpeg-gated). ---
    @pytest.mark.skipif(not _HAS_FFMPEG, reason='ffmpeg required to transcode m4a')
    def test_audio_m4a_normalizes_to_mp3(self, tmp_path):
        m4a = self._real_container(tmp_path, 'm4a')
        part = self._emit_audio(str(m4a))
        assert part['format'] == 'mp3'
        assert self._is_mp3_bytes(base64.b64decode(part['data']))

    @pytest.mark.skipif(not _HAS_FFMPEG, reason='ffmpeg required to transcode ogg')
    def test_audio_ogg_normalizes_to_mp3(self, tmp_path):
        ogg = self._real_container(tmp_path, 'ogg')
        part = self._emit_audio(str(ogg))
        assert part['format'] == 'mp3'
        assert self._is_mp3_bytes(base64.b64decode(part['data']))

    # --- Graceful degradation: no ffmpeg -> text error, no malformed input_audio. ---
    def test_audio_transcode_unavailable_degrades_gracefully(self, tmp_path, monkeypatch):
        import agent_cascade.utils.utils as _acutils
        from agent_cascade.llm.schema import ContentItem, Message
        # .ogg names a known non-decodable container -> transcode attempt; fake bytes
        # so no real decoding is required before the ffmpeg lookup fails.
        ogg = tmp_path / 'fake.ogg'
        ogg.write_bytes(b'OggSfakeogg')
        monkeypatch.setattr(_acutils.shutil, 'which', lambda _name: None)
        parts = self._adapter().convert_messages_to_dicts(
            [Message(role='user', content=[ContentItem(audio=str(ogg))])],
            generate_cfg={'audio_enabled': True},
        )[0]['content']
        assert [p for p in parts if p['type'] == 'input_audio'] == []
        texts = [p.get('text', '') for p in parts if p['type'] == 'text']
        assert any('System Error' in t for t in texts), parts

    def test_audio_malformed_data_uri_degrades_gracefully(self):
        # A malformed inline data-URI reaches base64.b64decode and raises binascii.Error
        # (a ValueError subclass); the widened except must degrade to [System Error],
        # not crash convert_messages_to_dicts.
        from agent_cascade.llm.schema import ContentItem, Message
        parts = self._adapter().convert_messages_to_dicts(
            [Message(role='user', content=[ContentItem(audio='data:audio/m4a;base64,A')])],
            generate_cfg={'audio_enabled': True},
        )[0]['content']
        assert [p for p in parts if p['type'] == 'input_audio'] == []
        texts = [p.get('text', '') for p in parts if p['type'] == 'text']
        assert any('System Error' in t for t in texts), parts
