import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.check_model_services as checker


class CheckModelServicesTest(unittest.TestCase):
    """验证本地小模型健康检查脚本的调用范围。"""

    @staticmethod
    def _real_health(**overrides):
        health = {
            "status": "ok",
            "service": "intelligent-meeting-local-model-service",
            "mockMode": False,
            "capabilities": {
                "vad": {"ready": True, "mode": "real"},
                "voiceprint": {"ready": True, "mode": "real", "embeddingReady": True},
                "alignment": {"ready": True, "mode": "real"},
            },
        }
        health.update(overrides)
        return health

    def test_default_check_only_reads_health_endpoint(self):
        calls: list[tuple[str, str]] = []

        def fake_request_json(method, url, payload=None, timeout=20):
            calls.append((method, url))
            return self._real_health()

        args = argparse.Namespace(base_url="http://127.0.0.1:8100", deep=False, audio_path="")

        with patch.object(checker, "_request_json", side_effect=fake_request_json):
            checker.run_checks(args)

        self.assertEqual(calls, [("GET", "http://127.0.0.1:8100/v1/health")])

    def test_deep_check_calls_core_model_endpoints(self):
        calls: list[tuple[str, str]] = []

        def fake_request_json(method, url, payload=None, timeout=20):
            calls.append((method, url))
            if url.endswith("/v1/health"):
                return self._real_health()
            if url.endswith("/v1/vad/split"):
                return {"segments": [{"start_ms": 0, "end_ms": 1000}]}
            if url.endswith("/v1/voiceprints/register"):
                return {"status": "registered", "embeddingId": "emb-1", "realModel": True, "fallbackReason": ""}
            if url.endswith("/v1/voiceprints/match"):
                return {"matches": [{"speakerName": "测试发言人"}], "realModel": True, "fallbackReason": ""}
            if url.endswith("/v1/speakers/embedding"):
                return {"embedding": [0.1, 0.2, 0.3], "realModel": True, "fallbackReason": ""}
            if url.endswith("/v1/align/selection-window"):
                return {"start_ms": 0, "end_ms": 1000, "words": []}
            return {}

        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "sample.wav"
            wav_path.write_bytes(b"fake wav")
            args = argparse.Namespace(base_url="http://127.0.0.1:8100", deep=True, audio_path=str(wav_path))

            with patch.object(checker, "_request_json", side_effect=fake_request_json):
                checker.run_checks(args)

        called_urls = "\n".join(url for _, url in calls)
        self.assertIn("/v1/health", called_urls)
        self.assertIn("/v1/vad/split", called_urls)
        self.assertIn("/v1/voiceprints/register", called_urls)
        self.assertIn("/v1/voiceprints/match", called_urls)
        self.assertIn("/v1/speakers/embedding", called_urls)
        self.assertIn("/v1/align/selection-window", called_urls)

    def test_check_rejects_mock_mode(self):
        args = argparse.Namespace(base_url="http://127.0.0.1:8100", deep=False, audio_path="")
        with patch.object(checker, "_request_json", return_value=self._real_health(mockMode=True)):
            with self.assertRaisesRegex(RuntimeError, "mock"):
                checker.run_checks(args)

    def test_deep_check_rejects_unready_capability_before_inference(self):
        args = argparse.Namespace(base_url="http://127.0.0.1:8100", deep=True, audio_path="")
        health = self._real_health(capabilities={"vad": {"ready": True, "mode": "real"}, "voiceprint": {"ready": False, "mode": "real"}, "alignment": {"ready": True, "mode": "real"}})
        with patch.object(checker, "_request_json", return_value=health):
            with self.assertRaisesRegex(RuntimeError, "voiceprint"):
                checker.run_checks(args)

    def test_default_check_also_rejects_unready_capability(self):
        """A shallow health check may skip inference calls, but it cannot certify unready models."""

        args = argparse.Namespace(base_url="http://127.0.0.1:8100", deep=False, audio_path="")
        health = self._real_health(
            capabilities={
                "vad": {"ready": True, "mode": "real"},
                "voiceprint": {"ready": False, "mode": "real"},
                "alignment": {"ready": True, "mode": "real"},
            }
        )
        with patch.object(checker, "_request_json", return_value=health):
            with self.assertRaisesRegex(RuntimeError, "voiceprint"):
                checker.run_checks(args)

    def test_deep_check_rejects_registered_fallback_without_real_model_marker(self):
        def fake_request_json(method, url, payload=None, timeout=20):
            if url.endswith("/v1/health"):
                return self._real_health()
            if url.endswith("/v1/vad/split"):
                return {"segments": []}
            if url.endswith("/v1/voiceprints/register"):
                return {"status": "registered", "embeddingId": "fallback", "fallbackReason": "weights missing"}
            return {}

        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "sample.wav"
            wav_path.write_bytes(b"fake wav")
            args = argparse.Namespace(base_url="http://127.0.0.1:8100", deep=True, audio_path=str(wav_path))
            with patch.object(checker, "_request_json", side_effect=fake_request_json):
                with self.assertRaisesRegex(RuntimeError, r"real CAM\+\+"):
                    checker.run_checks(args)

    def test_start_script_refuses_to_force_stop_an_unverified_port_owner(self):
        script = (Path(__file__).resolve().parents[2] / "scripts" / "start_model_services.ps1").read_text(encoding="utf-8")

        self.assertIn("function Test-ExpectedModelHealth", script)
        force_restart = script[script.index("if ($existingPid -and $ForceRestart)"):]
        self.assertLess(force_restart.index("Test-ExpectedModelHealth"), force_restart.index("Stop-Process"))

    def test_start_script_reuses_only_a_service_with_embedding_capability(self):
        """旧服务即使 status=ok，也不能因为缺少实时说话人 embedding 路由而被复用。"""

        script = (Path(__file__).resolve().parents[2] / "scripts" / "start_model_services.ps1").read_text(encoding="utf-8")
        self.assertIn("RequireEmbeddingCapability", script)
        self.assertIn("embeddingReady", script)


if __name__ == "__main__":
    unittest.main()
