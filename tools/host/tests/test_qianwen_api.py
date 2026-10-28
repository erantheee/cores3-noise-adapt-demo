"""Offline API contract and existing decision-path regression tests."""
import base64
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.error
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host import qianwen_api as api
from host.assistant import AssistantService
import qianwen_asr_worker as asr_worker


class QianwenTests(unittest.TestCase):
    def test_short_sparse_audio_uses_local_before_cloud(self):
        request = {
            "id": "short", "rate": 16000,
            "pcm_base64": base64.b64encode(b"\x01\x00" * 16000).decode(),
            "speech_seconds": 1.2,
        }
        fallback = unittest.mock.Mock()
        fallback.transcribe.return_value = "这是我的声音"
        with patch.object(asr_worker, "transcribe") as cloud:
            result = asr_worker.process_request(
                request, key="test", model="model", base="https://example.com",
                fallback=fallback,
            )
        self.assertEqual(result["text"], "这是我的声音")
        self.assertEqual(result["backend"], "local")
        self.assertEqual(result["fallback_reason"], "short_sparse")
        cloud.assert_not_called()

    def test_cloud_nospeech_uses_local_fallback(self):
        request = {
            "id": "dense", "rate": 16000,
            "pcm_base64": base64.b64encode(b"\x01\x00" * 64000).decode(),
            "speech_seconds": 4.0,
        }
        fallback = unittest.mock.Mock()
        fallback.transcribe.return_value = "我也是"
        with patch.object(asr_worker, "transcribe", side_effect=api.AsrNoSpeech("no speech")):
            result = asr_worker.process_request(
                request, key="test", model="model", base="https://example.com",
                fallback=fallback,
            )
        self.assertEqual(result["text"], "我也是")
        self.assertEqual(result["backend"], "local")
        self.assertEqual(result["fallback_reason"], "cloud_nospeech")

    def test_local_fallback_failure_preserves_cloud_nospeech(self):
        request = {
            "id": "none", "rate": 16000,
            "pcm_base64": base64.b64encode(b"\x00\x00" * 16000).decode(),
            "speech_seconds": 1.2,
        }
        fallback = unittest.mock.Mock()
        fallback.transcribe.return_value = None
        with patch.object(asr_worker, "transcribe", side_effect=api.AsrNoSpeech("no speech")):
            result = asr_worker.process_request(
                request, key="test", model="model", base="https://example.com",
                fallback=fallback,
            )
        self.assertEqual(result["type"], "transcript_nospeech")
        self.assertEqual(result["backend"], "qianwen")

    def test_chat_wire_contract(self):
        response = io.BytesIO(json.dumps({"model": api.CHAT_MODEL, "choices": [
            {"finish_reason": "stop", "message": {"content": '{"dialogue":"no"}'}}
        ]}).encode())
        with patch.object(api.urllib.request, "urlopen", return_value=response) as send:
            result = api.chat("JSON please", key="test-secret")
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, api.CHAT_BASE + "/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        body = json.loads(request.data)
        self.assertFalse(body["enable_thinking"])
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(result["content"][0]["text"], '{"dialogue":"no"}')

    def test_chat_rejects_empty_truncated_and_reasoning_only(self):
        for response in [{}, {"choices": []}, {"choices": [
            {"finish_reason": "length", "message": {"content": '{}'}}]},
            {"choices": [{"finish_reason": "stop", "message": {"reasoning_content": "yes"}}]}]:
            with self.subTest(response=response), patch.object(api, "post_json", return_value=response):
                with self.assertRaises(RuntimeError):
                    api.chat("JSON", key="test")

    def test_asr_wav_and_response_shapes(self):
        for response in [{"text": "你好"}, {"output": {"text": "你好"}},
                         {"output": {"output": {"sentence": {"text": "你好"}}}}]:
            with patch.object(api, "post_json", return_value=response) as send:
                self.assertEqual(api.transcribe(b'\x01\x00' * 160, 16000, key="test"), "你好")
            body = send.call_args.args[1]
            self.assertEqual(body["model"], "qwen-audio-3.0-asr-flash")
            uri = body["input"]["messages"][0]["content"][0]["input_audio"]["data"]
            with wave.open(io.BytesIO(base64.b64decode(uri.split(',')[1])), 'rb') as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 16000))
                self.assertEqual(wav.readframes(160), b'\x01\x00' * 160)

    def test_asr_empty_is_valid_missing_is_error(self):
        with patch.object(api, "post_json", return_value={"text": ""}):
            self.assertEqual(api.transcribe(b'\x00\x00', 16000, key="test"), "")
        with patch.object(api, "post_json", return_value={"output": {}}):
            with self.assertRaises(RuntimeError):
                api.transcribe(b'\x00\x00', 16000, key="test")

    def test_asr_http_400_means_no_speech(self):
        """该网关对静音/键盘/房间噪声回 400 空体；语义是"没人声"，不是接口故障。"""
        error = urllib.error.HTTPError(api.ASR_BASE, 400, 'Bad Request', {},
                                       io.BytesIO(b'{}'))
        with patch.object(api.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(api.AsrNoSpeech):
                api.transcribe(b'\x01\x00' * 160, 16000, key="test")
        error500 = urllib.error.HTTPError(api.ASR_BASE, 500, 'Oops', {},
                                          io.BytesIO(b''))
        with patch.object(api.urllib.request, "urlopen", side_effect=error500):
            with self.assertRaises(api.ProviderHTTPError) as caught:
                api.transcribe(b'\x01\x00' * 160, 16000, key="test")
            self.assertEqual(caught.exception.code, 500)

    def test_bad_audio_never_sent(self):
        with patch.object(api, "post_json") as send:
            for pcm, rate in [(b'', 16000), (b'x', 16000), (b'xx', 0)]:
                with self.assertRaises(ValueError):
                    api.transcribe(pcm, rate, key="test")
            send.assert_not_called()

    def test_no_key_or_insecure_url_never_sent(self):
        with patch.object(api.urllib.request, "urlopen") as send:
            with self.assertRaises(RuntimeError):
                api.post_json(api.CHAT_BASE, {}, "", 1)
            with self.assertRaises(ValueError):
                api.post_json('http://example.com', {}, "test", 1)
            send.assert_not_called()

    def test_errors_redact_key_and_body(self):
        errors = [urllib.error.URLError('secret-value'),
                  urllib.error.HTTPError(api.CHAT_BASE, 401, 'secret-value', {}, io.BytesIO(b'secret-value'))]
        for error in errors:
            with patch.object(api.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    api.post_json(api.CHAT_BASE, {}, 'secret-value', 1)
                self.assertNotIn('secret-value', str(caught.exception))

    def test_provider_selection_and_probe(self):
        with patch.dict(os.environ, {"CORES3_LLM_PROVIDER": "qianwen", "CORES3_ASR_PROVIDER": "qianwen",
                                    "DASHSCOPE_API_KEY": "test", "ANTHROPIC_MODEL": "old"}, clear=True):
            service = AssistantService()
        self.assertEqual(service.model, api.CHAT_MODEL)
        self.assertEqual(service._asr_model, api.ASR_MODEL)
        with patch('host.assistant.chat', return_value={"model": api.CHAT_MODEL}) as chat:
            service._probe()
            self.assertFalse(chat.call_args.kwargs['structured'])
        self.assertEqual(service.snapshot()['state'], 'ready')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(AssistantService().provider, 'anthropic')

    def test_asr_failure_is_unknown_even_with_partial_transcript(self):
        from types import SimpleNamespace
        import time
        for partial in [False, True]:
            with patch.dict(os.environ, {}, clear=True):
                service = AssistantService()
            service._pending = {'a': ('event', 0, 'self', 2.0, time.monotonic())}
            messages = [{'type': 'transcript_error', 'id': 'a', 'error': 'HTTP 400'}]
            if partial:
                service._pending['b'] = ('event', 1, 'other', 2.0, time.monotonic())
                messages.append({'type': 'transcript', 'id': 'b', 'text': '我们明天再讨论这个问题'})
            service._pending_events = {'event': {
                'number': 1, 'seconds': 4, 'chunks': 1, 'chunks_lost': 0, 'telemetry': [],
                'remaining': len(messages), 'results': {}, 'epoch_id': 0,
            }}
            service._asr_process = SimpleNamespace(stdout=io.StringIO(
                '\n'.join(json.dumps(m) for m in messages)+'\n'))
            with patch.object(service, '_publish_decision') as publish, patch.object(service, '_summarize_event') as llm:
                service._read_asr()
                self.assertEqual(publish.call_args.args[1:3], ('unclear', 'close'))
                self.assertIn('HTTP 400', publish.call_args.args[4])
                llm.assert_not_called()
            self.assertFalse(service._pending_events)

    def test_existing_decision_path_and_failure(self):
        with patch.dict(os.environ, {"CORES3_LLM_PROVIDER": "qianwen", "DASHSCOPE_API_KEY": "test"}, clear=True):
            service = AssistantService()
        raw = json.dumps({"dialogue": "yes", "ambient": "open", "confidence": .9, "reason": "双方问答"})
        response = {"model": api.CHAT_MODEL, "content": [{"type": "text", "text": raw}]}
        with patch('host.assistant.chat', return_value=response), patch.object(service, '_publish_decision') as publish:
            service._summarize_event(1, 3, 1, 0, [], '你好', [('self','你好'),('other','你好')])
            self.assertEqual(publish.call_args.args[1:3], ('yes','open'))
        for outcome in [RuntimeError('HTTP 401'), {"content": [{"type":"text", "text":"invalid"}]}]:
            kwargs = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
            with patch('host.assistant.chat', **kwargs), patch.object(service, '_publish_decision') as publish:
                service._summarize_event(2, 3, 1, 0, [], '你好')
                self.assertEqual(publish.call_args.args[1:3], ('unclear','close'))


if __name__ == '__main__':
    unittest.main()
