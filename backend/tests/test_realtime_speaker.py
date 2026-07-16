import unittest

from app.realtime_speaker import (
    REALTIME_FINAL_SEGMENT_CLUSTER_COSINE_THRESHOLD,
    RealtimeSpeakerTracker,
)


class RealtimeSpeakerTrackerTest(unittest.TestCase):
    """验证单场会议内的说话人身份只由内存中的 embedding 状态决定。"""

    def test_close_vectors_reuse_first_anonymous_cluster(self):
        tracker = RealtimeSpeakerTracker()

        first = tracker.identify([1.0, 0.0, 0.0])
        second = tracker.identify([0.99, 0.04, 0.0])

        # 两个余弦方向非常接近的片段必须复用同一个首次出现编号，避免逐句跳号。
        self.assertEqual(first.speaker_name, "发言人1")
        self.assertEqual(second.speaker_name, "发言人1")
        self.assertEqual(second.speaker_cluster_id, first.speaker_cluster_id)
        self.assertEqual(second.speaker_source, "anonymous_cluster")

    def test_distant_vector_creates_second_anonymous_cluster(self):
        tracker = RealtimeSpeakerTracker()

        first = tracker.identify([1.0, 0.0, 0.0])
        second = tracker.identify([0.0, 1.0, 0.0])

        # 正交向量远低于默认聚类阈值，应按首次出现顺序创建第二个会议内身份。
        self.assertEqual(first.speaker_name, "发言人1")
        self.assertEqual(second.speaker_name, "发言人2")
        self.assertNotEqual(second.speaker_cluster_id, first.speaker_cluster_id)
        # 会后整段模型只应使用真正取得 embedding 的人数证据，不把无模型 fallback 计入。
        self.assertEqual(tracker.evidence_speaker_count, 2)

    def test_fallback_without_embedding_is_not_speaker_count_evidence(self):
        """模型不可用时不能把公共 fallback 当成已确认的一人会议。"""

        tracker = RealtimeSpeakerTracker()
        tracker.identify(None)

        self.assertEqual(tracker.evidence_speaker_count, 0)

    def test_realtime_final_segment_threshold_prevents_similarity_chain_collapse(self):
        """较长最终句不能通过低阈值质心链把三种明显声音吞并成一个身份。"""

        tracker = RealtimeSpeakerTracker(
            cluster_threshold=REALTIME_FINAL_SEGMENT_CLUSTER_COSINE_THRESHOLD,
        )
        # 这些小向量保持与真实 26 秒录音相同的关键关系：第二、三句相近，第四、六句
        # 相近，第五句回到第二人；第一段系统声与两位人物均不应合并。
        vectors = [
            [1.0, 0.0, 0.0],
            [0.33, 0.944, 0.0],
            [0.45, 0.89, 0.0],
            [0.11, 0.0, 0.994],
            [0.46, 0.88, 0.0],
            [0.16, 0.10, 0.98],
        ]

        speakers = [tracker.identify(vector).speaker_name for vector in vectors]

        self.assertEqual(speakers[0], "发言人1")
        self.assertEqual(speakers[1], speakers[2])
        self.assertEqual(speakers[1], speakers[4])
        self.assertEqual(speakers[3], speakers[5])
        self.assertEqual(len(set(speakers)), 3)

    def test_short_campp_same_speaker_similarity_does_not_increment_every_sentence(self):
        """短句 CAM++ 的同人相似度远低于长音频，默认阈值必须覆盖真实校准区间。"""

        tracker = RealtimeSpeakerTracker()

        first_speaker = tracker.identify([1.0, 0.0, 0.0, 0.0])
        second_speaker = tracker.identify([0.0, 1.0, 0.0, 0.0])
        # 该向量与发言人1的余弦为 0.32、与发言人2为 0.10，模拟真实录音中
        # 0.75～1.91 秒短句的 CAM++ 波动。旧默认阈值 0.78 会错误创建发言人3。
        first_speaker_again = tracker.identify([0.32, 0.10, 0.9425, 0.0])

        self.assertEqual(first_speaker.speaker_name, "发言人1")
        self.assertEqual(second_speaker.speaker_name, "发言人2")
        self.assertEqual(first_speaker_again.speaker_name, "发言人1")
        self.assertEqual(
            first_speaker_again.speaker_cluster_id,
            first_speaker.speaker_cluster_id,
        )

    def test_clearly_new_third_speaker_is_not_hidden_by_lower_short_audio_threshold(self):
        """降低短句阈值只修复同人漂移，明显不同的第三人仍必须获得独立身份。"""

        tracker = RealtimeSpeakerTracker()
        tracker.identify([1.0, 0.0, 0.0])
        tracker.identify([0.0, 1.0, 0.0])
        third = tracker.identify([0.0, 0.0, 1.0])

        self.assertEqual(third.speaker_name, "发言人3")
        self.assertEqual(third.speaker_cluster_id, "speaker-3")

    def test_accepted_voiceprint_match_has_priority_over_anonymous_name(self):
        tracker = RealtimeSpeakerTracker()

        identity = tracker.identify(
            [1.0, 0.0, 0.0],
            voiceprint_match={
                "speakerName": "王总",
                "speakerId": "vp-001",
                "confidence": 0.91,
                "speakerTitle": "研发总监",
            },
        )

        self.assertEqual(identity.speaker_name, "王总")
        self.assertEqual(identity.voiceprint_id, "vp-001")
        self.assertEqual(identity.speaker_source, "voiceprint")
        self.assertEqual(identity.speaker_title, "研发总监")
        self.assertAlmostEqual(identity.confidence, 0.91)

    def test_later_voiceprint_hit_upgrades_existing_anonymous_cluster(self):
        tracker = RealtimeSpeakerTracker()
        anonymous = tracker.identify([1.0, 0.0, 0.0])

        upgraded = tracker.identify(
            [0.99, 0.02, 0.0],
            voiceprint_match={
                "speakerName": "李经理",
                "voiceprintId": "vp-002",
                "confidence": 0.88,
            },
        )
        remembered = tracker.identify([0.98, 0.03, 0.0])

        # 命中声纹后仍保留原聚类 ID，供后续任务按该 ID 修正历史匿名片段。
        self.assertEqual(upgraded.speaker_cluster_id, anonymous.speaker_cluster_id)
        self.assertEqual(upgraded.speaker_name, "李经理")
        self.assertEqual(remembered.speaker_name, "李经理")
        self.assertEqual(remembered.voiceprint_id, "vp-002")
        self.assertEqual(remembered.speaker_source, "voiceprint")

    def test_same_voiceprint_reuses_canonical_cluster_even_for_distant_embedding(self):
        tracker = RealtimeSpeakerTracker()

        first = tracker.identify(
            [1.0, 0.0, 0.0],
            voiceprint_match={
                "speakerName": "王总",
                "voiceprintId": "vp-canonical",
                "confidence": 0.93,
            },
        )
        distant = tracker.identify(
            [0.0, 1.0, 0.0],
            voiceprint_match={
                "speakerName": "王总",
                "voiceprintId": "vp-canonical",
                "confidence": 0.90,
            },
        )

        # 声纹库 ID 是会议内强身份键；一旦建立 canonical cluster，向量漂移不能再创建别名 cluster。
        self.assertEqual(distant.speaker_cluster_id, first.speaker_cluster_id)
        self.assertEqual(distant.voiceprint_id, "vp-canonical")
        self.assertEqual(distant.speaker_name, "王总")

    def test_different_voiceprints_never_overwrite_one_nearby_confirmed_cluster(self):
        tracker = RealtimeSpeakerTracker()

        first = tracker.identify(
            [1.0, 0.0, 0.0],
            voiceprint_match={
                "speakerName": "张总",
                "voiceprintId": "vp-zhang",
                "confidence": 0.94,
            },
        )
        second = tracker.identify(
            [0.999, 0.01, 0.0],
            voiceprint_match={
                "speakerName": "李总",
                "voiceprintId": "vp-li",
                "confidence": 0.92,
            },
        )
        first_again = tracker.identify(
            [0.0, 1.0, 0.0],
            voiceprint_match={
                "speakerName": "张总",
                "voiceprintId": "vp-zhang",
                "confidence": 0.91,
            },
        )

        # embedding 极近也不能让另一个已接受 voiceprint 改写 cluster 的姓名和归属。
        self.assertNotEqual(second.speaker_cluster_id, first.speaker_cluster_id)
        self.assertEqual(second.voiceprint_id, "vp-li")
        self.assertEqual(second.speaker_name, "李总")
        self.assertEqual(first_again.speaker_cluster_id, first.speaker_cluster_id)
        self.assertEqual(first_again.voiceprint_id, "vp-zhang")
        self.assertEqual(first_again.speaker_name, "张总")

    def test_voiceprint_upgraded_fallback_becomes_its_canonical_cluster(self):
        tracker = RealtimeSpeakerTracker()

        fallback = tracker.identify(
            None,
            voiceprint_match={
                "speakerName": "赵老师",
                "voiceprintId": "vp-fallback",
                "confidence": 0.89,
            },
        )
        later = tracker.identify(
            [0.0, 0.0, 1.0],
            voiceprint_match={
                "speakerName": "赵老师",
                "voiceprintId": "vp-fallback",
                "confidence": 0.90,
            },
        )

        # 首次没有 embedding 不影响强身份绑定；后续恢复向量后仍必须回到同一 cluster。
        self.assertEqual(later.speaker_cluster_id, fallback.speaker_cluster_id)
        self.assertEqual(later.voiceprint_id, "vp-fallback")
        self.assertEqual(later.speaker_name, "赵老师")

    def test_existing_anonymous_fallback_is_promoted_before_valid_voiceprint_embedding(self):
        tracker = RealtimeSpeakerTracker()

        fallback = tracker.identify(None, None)
        promoted = tracker.identify(
            [1.0, 0.0, 0.0],
            voiceprint_match={
                "speakerName": "周主任",
                "voiceprintId": "vp-promoted-fallback",
                "confidence": 0.92,
            },
        )
        distant = tracker.identify(
            [0.0, 1.0, 0.0],
            voiceprint_match={
                "speakerName": "周主任",
                "voiceprintId": "vp-promoted-fallback",
                "confidence": 0.90,
            },
        )

        # 第一步已经把当前未知发言人暴露为 speaker-1；第二步拿到可信声纹和有效向量时
        # 必须原位升级该 fallback，不能另建 speaker-2。canonical 绑定建立后，第三步即使
        # embedding 与首次方向相距很远，也必须先按 voiceprint_id 回到同一个 speaker-1。
        self.assertEqual(fallback.speaker_cluster_id, "speaker-1")
        self.assertEqual(promoted.speaker_cluster_id, "speaker-1")
        self.assertEqual(distant.speaker_cluster_id, "speaker-1")
        self.assertEqual(promoted.voiceprint_id, "vp-promoted-fallback")
        self.assertEqual(distant.voiceprint_id, "vp-promoted-fallback")
        self.assertEqual(promoted.speaker_name, "周主任")
        self.assertEqual(distant.speaker_name, "周主任")
        # SpeakerIdentity 是业务边界对象；三次返回都不得因 fallback 升级而携带私有向量。
        self.assertNotIn("embedding", vars(fallback))
        self.assertNotIn("embedding", vars(promoted))
        self.assertNotIn("embedding", vars(distant))

    def test_low_confidence_voiceprint_never_displays_registered_name(self):
        tracker = RealtimeSpeakerTracker(voiceprint_threshold=0.75)

        identity = tracker.identify(
            [1.0, 0.0, 0.0],
            voiceprint_match={
                "speakerName": "不应显示的姓名",
                "voiceprintId": "vp-low",
                "confidence": 0.749,
            },
        )

        self.assertEqual(identity.speaker_name, "发言人1")
        self.assertEqual(identity.speaker_source, "anonymous_cluster")
        self.assertIsNone(identity.voiceprint_id)

    def test_empty_vectors_use_one_stable_fallback_identity_without_embedding_output(self):
        tracker = RealtimeSpeakerTracker()

        first = tracker.identify([])
        second = tracker.identify(None)

        # 模型临时不可用时文本链路仍需稳定；公开身份对象不得携带原始 embedding。
        self.assertEqual(first.speaker_name, "发言人1")
        self.assertEqual(second.speaker_name, "发言人1")
        self.assertEqual(second.speaker_cluster_id, first.speaker_cluster_id)
        self.assertNotIn("embedding", vars(first))


if __name__ == "__main__":
    unittest.main()
