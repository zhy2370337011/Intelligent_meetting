"""Focused TDD coverage for the service-level smoke capability report.

These tests exercise only reporter semantics, so the normal local suite never needs a running
ASR, VAD, CAM++, or ForcedAligner service.  The product smoke command still probes live services
when an operator runs it explicitly.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
import wave

import scripts.smoke_verify_system as smoke


class SmokeCapabilityReportTest(unittest.TestCase):
    """Keep the machine-readable readiness contract truthful and independently testable."""

    def test_mock_or_unavailable_models_are_degraded_instead_of_ready(self) -> None:
        """A successful mock process must never certify CAM++ or ForcedAligner as real inference.

        The model-service endpoint can be reachable while individual weights are unavailable.
        In particular, the report must preserve an unavailable ForcedAligner as ``degraded`` even
        when unrelated mocked checks pass, so operators do not mistake unit-test coverage for
        production model readiness.
        """

        report = smoke.new_capability_report()

        smoke.record_model_capabilities(
            report,
            {
                "vad": {"ready": True, "mode": "real", "message": "FSMN-VAD ready"},
                "voiceprint": {"ready": False, "mode": "mock", "message": "mock CAM++"},
                "alignment": {"ready": False, "mode": "unavailable", "message": "weights missing"},
            },
        )

        self.assertEqual(report["vad"]["status"], "ready")
        self.assertEqual(report["voiceprint"]["status"], "degraded")
        self.assertEqual(report["alignment"]["status"], "degraded")
        self.assertIn("weights missing", report["alignment"]["message"])

    def test_writer_creates_a_utf8_machine_readable_report(self) -> None:
        """The persisted report keeps all subsystem status values and UTF-8 diagnostic text."""

        report = smoke.new_capability_report()
        smoke.record_capability(report, "import", "ready", mode="import", message="离线导入已验证")

        with TemporaryDirectory() as directory:
            output_path = Path(directory) / "nested" / "capability-report.json"
            smoke.write_capability_report(report, output_path)
            persisted = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(persisted["import"]["status"], "ready")
        self.assertEqual(persisted["import"]["message"], "离线导入已验证")

    def test_realtime_smoke_streams_pcm_and_collects_a_final_before_close(self) -> None:
        """The realtime verifier must exercise the WebSocket protocol, not reuse import evidence."""

        class FakeSocket:
            def __init__(self) -> None:
                self.sent: list[str | bytes] = []
                self.received = iter(
                    [
                        json.dumps({"type": "status", "code": "streaming_started"}),
                        json.dumps({"type": "partial_transcript", "text": "正在识别"}),
                        json.dumps({"type": "transcript", "segment": {"text": "能不能实时识别"}}),
                        json.dumps({"type": "closed"}),
                    ]
                )

            async def send(self, payload: str | bytes) -> None:
                self.sent.append(payload)

            async def recv(self) -> str:
                await asyncio.sleep(0)
                return next(self.received)

        class FakeConnection:
            def __init__(self, socket: FakeSocket) -> None:
                self.socket = socket

            async def __aenter__(self) -> FakeSocket:
                return self.socket

            async def __aexit__(self, *_args) -> None:
                return None

        socket = FakeSocket()

        def connect_factory(url: str, **_kwargs) -> FakeConnection:
            self.assertTrue(url.endswith("/api/meetings/realtime-smoke/realtime"))
            return FakeConnection(socket)

        with TemporaryDirectory() as directory:
            wav_path = Path(directory) / "known.wav"
            with wave.open(str(wav_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(struct.pack("<h", 4000) * 1600)
            result = asyncio.run(
                smoke._exercise_realtime_stream(
                    "http://backend.test",
                    "realtime-smoke",
                    wav_path,
                    connect_factory=connect_factory,
                )
            )

        config = json.loads(str(socket.sent[0]))
        binary_frames = [payload for payload in socket.sent if isinstance(payload, bytes)]
        self.assertEqual(config["streamingMode"], "dashscope_realtime")
        self.assertTrue(binary_frames)
        self.assertFalse(binary_frames[0].startswith(b"RIFF"))
        self.assertEqual(socket.sent[-1], "stop")
        self.assertIn("能不能实时识别", result["transcriptText"])
        self.assertEqual(result["audioDurationMs"], 100)


if __name__ == "__main__":
    unittest.main()
