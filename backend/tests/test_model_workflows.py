import unittest

from app.asr_gateway import MockQwenAsrGateway
from app.llm_workflow import generate_mock_meeting_minutes, generate_mock_summary


class ModelWorkflowTest(unittest.TestCase):
    def test_mock_qwen_asr_returns_segments_with_speaker_and_time(self):
        gateway = MockQwenAsrGateway()

        result = gateway.transcribe_offline(
            meeting_id="m-001",
            file_id="file-001",
            enable_diarization=True,
            hotwords=["Qwen3-ASR", "910B"],
            sensitive_words=["糟糕"],
            start_ms=0,
            end_ms=60000,
        )

        self.assertEqual(result["model"], "Qwen3-ASR-1.7B")
        self.assertEqual(result["fileId"], "file-001")
        self.assertGreaterEqual(len(result["segments"]), 2)
        self.assertIn("speakerName", result["segments"][0])
        self.assertIn("words", result["segments"][0])
        # ASR 网关保存可追溯的原始识别文本；敏感词只在冻结策略的 display/AI/export
        # 目标层生成脱敏视图。网关阶段直接替换会破坏后续审计、规则变更和重新导出。
        self.assertIn("糟糕", "".join(item["text"] for item in result["segments"]))

    def test_mock_summary_contains_required_meeting_sections(self):
        summary = generate_mock_summary(
            [
                {"speakerName": "张三", "text": "请信息中心完成 ASR 服务联调。"},
                {"speakerName": "李四", "text": "办公室负责整理会议纪要模板。"},
            ]
        )

        self.assertIn("keywords", summary)
        self.assertIn("topic", summary)
        self.assertIn("keyPoints", summary)
        self.assertIn("todos", summary)
        self.assertGreaterEqual(len(summary["todos"]), 1)

    def test_mock_minutes_uses_template_name_and_transcript(self):
        minutes = generate_mock_meeting_minutes(
            meeting_name="智能会议建设会",
            template_name="标准会议纪要",
            summary={"topic": "智能会议系统建设", "keyPoints": ["完成模型网关"]},
        )

        self.assertIn("标准会议纪要", minutes["templateName"])
        self.assertIn("智能会议建设会", minutes["title"])
        self.assertIn("完成模型网关", minutes["content"])


if __name__ == "__main__":
    unittest.main()
