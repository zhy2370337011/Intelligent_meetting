import argparse
import unittest
from unittest.mock import patch

import scripts.smoke_verify_system as smoke


class SmokeVerifySystemTest(unittest.TestCase):
    """验证本地冒烟脚本覆盖智慧会议前端会触发的关键后端能力。"""

    def test_smoke_script_checks_translate_discourse_and_docx_export(self):
        """冒烟脚本必须覆盖 5 个 AI 工具中的翻译、语篇规整，以及 docx 导出。

        摘要、纪要、待办原脚本已经覆盖；这条测试补上剩余接口，避免“脚本通过但前端按钮仍坏”的盲区。
        """

        calls: list[tuple[str, str]] = []

        def fake_request_json(method, url, payload=None, timeout=20):
            calls.append((method, url))
            if url.endswith("/api/health"):
                return {"status": "ok", "asrGatewayMode": "mock", "modelMockMode": True}
            if url.endswith("/api/workflows/status"):
                return {"workflows": {}, "mode": "mock", "allWorkflowIdsConfigured": False}
            if url.endswith("/v1/health"):
                return {"status": "ok", "models": {}}
            if url.endswith("/api/meetings") and method == "GET":
                return {"items": [], "total": 0}
            if url.endswith("/api/dashboard/overview"):
                return {
                    "items": [
                        {"key": "todayMeetings"},
                        {"key": "readyMinutes"},
                        {"key": "pendingTodos"},
                    ]
                }
            if url.endswith("/api/meetings") and method == "POST":
                return {"id": "m-smoke"}
            if url.endswith("/api/meetings/m-smoke"):
                return {"id": "m-smoke"}
            if url.endswith("/api/dictionaries/keyword-libraries"):
                return {"id": "kw-smoke"}
            if url.endswith("/api/dictionaries/sensitive-rules"):
                return {"id": "sw-smoke"}
            if url.endswith("/api/minute-templates?source=system"):
                return {"items": [{"id": "tpl-system"}]}
            if url.endswith("/api/voiceprints"):
                return {"id": "vp-smoke"}
            if url.endswith("/api/meetings/m-smoke/jobs"):
                return {"items": []}
            if url.endswith("/summaries/generate"):
                return {"keywords": ["智能会议"]}
            if url.endswith("/minutes/generate"):
                return {"content": "会议纪要正文"}
            if url.endswith("/todos/extract"):
                return {"items": []}
            if url.endswith("/translate"):
                return {"text": "Please complete integration."}
            if url.endswith("/discourse/reorganize"):
                return {"text": "规整后的会议正文"}
            if method == "DELETE":
                return {"deleted": True}
            return {}

        def fake_multipart_upload(url, file_field, file_path, timeout=60):
            calls.append(("UPLOAD", url))
            if "/api/voiceprints/" in url:
                return {"voiceprint": {"id": "vp-smoke", "registerStatus": "registered"}}
            return {"id": "file-smoke"}

        def fake_request_bytes(method, url, payload=None, timeout=30):
            calls.append((method, url))
            return b"docx-bytes" * 16

        args = argparse.Namespace(
            backend="http://backend.test",
            frontend="http://frontend.test",
            model_service="http://model.test",
            include_asr=False,
        )

        with patch.object(smoke, "_get_text", return_value="<title>智能会议系统</title>"), patch.object(
            smoke, "_request_json", side_effect=fake_request_json
        ), patch.object(smoke, "_multipart_upload", side_effect=fake_multipart_upload), patch.object(
            smoke, "_request_bytes", side_effect=fake_request_bytes
        ):
            smoke.run_smoke(args)

        called_urls = "\n".join(url for _, url in calls)
        self.assertIn("/api/meetings/m-smoke/translate", called_urls)
        self.assertIn("/api/meetings/m-smoke/discourse/reorganize", called_urls)
        self.assertIn("/api/meetings/m-smoke/exports/docx", called_urls)


if __name__ == "__main__":
    unittest.main()
