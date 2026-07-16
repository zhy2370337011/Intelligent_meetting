"""会议级实时说话人身份跟踪。

本模块只在单个实时会议会话的内存中保存 CAM++ embedding 质心。对外返回的
``SpeakerIdentity`` 仅包含可持久化的身份字段，原始向量不会进入会议字典、API 响应或浏览器。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


# 匿名聚类使用“短句 CAM++”实测阈值，而不能照搬长音频声纹验证阈值。本项目真实录音经
# VAD 切成 0.75～1.91 秒片段后，同一人的余弦相似度只有 0.32～0.58；旧值 0.78 会把
# 每个短句都误判成新人，表现为发言人编号持续递增。0.30 能让上述同人片段回到同一簇，
# 同时正交或负相关的明显不同声音仍会创建新簇。声纹库姓名继续使用下方独立的 0.75
# 高阈值，降低匿名阈值不会把注册人员姓名误显示给用户。
DEFAULT_ANONYMOUS_CLUSTER_COSINE_THRESHOLD = 0.30

# 实时 ASR 最终片段通常覆盖 2～9 秒，信息量显著高于上面的亚秒级/短句兼容场景。对用户
# 2026-07-16 的 26 秒真实录音逐段提取 CAM++ 后，0.30 会因“甲接近乙、乙又接近丙”的
# 质心链式效应把系统声和两位人物吞并成一人；0.58 则得到稳定的 1/2/2/3/2/3 分组。
# 该常量仅供实时最终片段使用，通用 tracker 默认值保持不变，避免影响注册声纹和既有短音频调用。
REALTIME_FINAL_SEGMENT_CLUSTER_COSINE_THRESHOLD = 0.58

# 声纹库姓名会直接显示给用户，因此接受阈值与匿名聚类分开管理。低于 0.75 的候选即使
# 带有姓名也按未命中处理，避免把库中人员姓名错误展示在逐字稿中。
DEFAULT_VOICEPRINT_ACCEPTANCE_THRESHOLD = 0.75


@dataclass(frozen=True)
class SpeakerIdentity:
    """可离开 tracker 边界的说话人身份，不包含任何 embedding 数据。"""

    speaker_name: str
    speaker_cluster_id: str
    speaker_source: str
    voiceprint_id: str | None
    confidence: float
    speaker_title: str | None = None


@dataclass
class _SpeakerCluster:
    """单场会议内的私有聚类状态；``centroid`` 绝不能序列化到业务对象。"""

    cluster_id: str
    anonymous_name: str
    centroid: list[float] | None
    sample_count: int
    speaker_name: str
    speaker_source: str = "anonymous_cluster"
    voiceprint_id: str | None = None
    voiceprint_confidence: float = 0.0
    speaker_title: str | None = None


def _normalize_embedding(embedding: Iterable[float] | None) -> list[float] | None:
    """将输入规整为单位向量；空、非数值、非有限或零向量统一视为不可用。"""

    if embedding is None:
        return None
    try:
        values = [float(value) for value in embedding]
    except (TypeError, ValueError):
        return None
    if not values or any(not math.isfinite(value) for value in values):
        return None
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0.0:
        return None
    return [value / norm for value in values]


def _cosine(left: list[float] | None, right: list[float] | None) -> float:
    """计算同维单位向量的余弦相似度，不可比较时返回 -1 避免误命中。"""

    if left is None or right is None or len(left) != len(right):
        return -1.0
    return sum(a * b for a, b in zip(left, right))


class RealtimeSpeakerTracker:
    """为一场会议分配稳定匿名编号，并允许可信声纹命中升级该编号。"""

    def __init__(
        self,
        cluster_threshold: float = DEFAULT_ANONYMOUS_CLUSTER_COSINE_THRESHOLD,
        voiceprint_threshold: float = DEFAULT_VOICEPRINT_ACCEPTANCE_THRESHOLD,
    ) -> None:
        self.cluster_threshold = float(cluster_threshold)
        self.voiceprint_threshold = float(voiceprint_threshold)
        self._clusters: list[_SpeakerCluster] = []
        self._fallback_cluster: _SpeakerCluster | None = None
        # voiceprint_id 是已登记人员在单场会议中的强身份键。该索引保证同一 ID 无论
        # embedding 漂移多远、甚至首次没有 embedding，都只解析到一个 canonical cluster。
        self._voiceprint_clusters: dict[str, _SpeakerCluster] = {}
        # cluster 序号独立递增，避免没有质心、未加入向量检索列表的 fallback cluster 导致 ID 重复。
        self._next_cluster_ordinal = 1

    @property
    def evidence_speaker_count(self) -> int:
        """返回已经获得有效 embedding 证据的会议内身份数量。

        无向量的 fallback 仅表示“模型暂不可用”，不能据此推断会议里只有一人；因此这里只
        统计真正进入向量检索列表的 cluster。会后 3D-Speaker 可把该数量作为人数先验，但
        原始向量和质心仍然留在 tracker 内存中，不会进入会议数据或接口响应。
        """

        return len(self._clusters)

    def identify(
        self,
        embedding: Iterable[float] | None,
        voiceprint_match: Mapping[str, Any] | None = None,
    ) -> SpeakerIdentity:
        """返回当前片段身份，并在本会议内增量更新最接近的聚类质心。

        决策顺序是：先验证声纹候选是否达到显示阈值，再寻找/创建会议内聚类，最后将
        可信声纹身份写到该聚类。这样后续没有再次命中声纹库的相近片段仍会沿用已确认姓名。
        """

        vector = _normalize_embedding(embedding)
        accepted_match = self._accepted_voiceprint_match(voiceprint_match)

        if accepted_match is not None:
            voiceprint_id = accepted_match.get("voiceprint_id")
            canonical = self._voiceprint_clusters.get(voiceprint_id) if voiceprint_id else None
            if canonical is not None:
                # 强身份命中必须先于匿名聚类。即使本次向量与历史质心很远，也只能更新
                # 该 voiceprint 已绑定的 canonical cluster，不能创建第二个会议内身份。
                self._attach_vector(canonical, vector)
                self._upgrade_cluster(canonical, accepted_match)
                return self._public_identity(canonical)

            # 新 voiceprint 只能升级尚未绑定的匿名 cluster。若最近向量 cluster 已属于
            # 另一个 voiceprint，则忽略它并创建自己的 canonical cluster，避免静默改名。
            cluster = self._resolve_new_voiceprint_cluster(vector)
            self._upgrade_cluster(cluster, accepted_match)
            if voiceprint_id:
                self._voiceprint_clusters[voiceprint_id] = cluster
            if cluster is self._fallback_cluster:
                # 已绑定声纹的无向量 cluster 不再作为普通“模型不可用”片段的公共 fallback；
                # 只有携带同一 voiceprint_id 的后续命中可以通过 canonical 索引复用它。
                self._fallback_cluster = None
            return self._public_identity(cluster)

        if vector is None:
            # 无向量且未命中声纹时使用当前未绑定 fallback，保证文本链路稳定且不冒用姓名。
            cluster = self._fallback_identity()
        else:
            cluster, similarity = self._closest_cluster(vector)
            if cluster is None or similarity < self.cluster_threshold:
                cluster = self._new_cluster(vector)
            else:
                self._update_centroid(cluster, vector)
        return self._public_identity(cluster)

    def _accepted_voiceprint_match(
        self, voiceprint_match: Mapping[str, Any] | None
    ) -> dict[str, Any] | None:
        """只接受达到阈值且带有效姓名的候选，低分姓名不会泄漏到显示层。"""

        if not voiceprint_match:
            return None
        try:
            confidence = float(voiceprint_match.get("confidence", 0.0))
        except (TypeError, ValueError):
            return None
        speaker_name = str(voiceprint_match.get("speakerName") or "").strip()
        if not speaker_name or not math.isfinite(confidence) or confidence < self.voiceprint_threshold:
            return None
        return {
            "speaker_name": speaker_name,
            "voiceprint_id": str(
                voiceprint_match.get("voiceprintId")
                or voiceprint_match.get("speakerId")
                or voiceprint_match.get("embeddingId")
                or ""
            ).strip()
            or None,
            "confidence": confidence,
            "speaker_title": str(
                voiceprint_match.get("speakerTitle")
                or voiceprint_match.get("title")
                or ""
            ).strip()
            or None,
        }

    def _closest_cluster(
        self,
        vector: list[float],
        *,
        anonymous_only: bool = False,
    ) -> tuple[_SpeakerCluster | None, float]:
        """返回最高相似 cluster；可限制为尚未绑定声纹的匿名候选。"""

        best_cluster: _SpeakerCluster | None = None
        best_similarity = -1.0
        for cluster in self._clusters:
            if anonymous_only and cluster.speaker_source != "anonymous_cluster":
                continue
            similarity = _cosine(vector, cluster.centroid)
            if similarity > best_similarity:
                best_cluster = cluster
                best_similarity = similarity
        return best_cluster, best_similarity

    def _resolve_new_voiceprint_cluster(self, vector: list[float] | None) -> _SpeakerCluster:
        """为首次出现的 voiceprint 解析一个未绑定 cluster，不触碰其它已确认身份。"""

        if (
            self._fallback_cluster is not None
            and self._fallback_cluster.speaker_source == "anonymous_cluster"
        ):
            # fallback 已经作为当前未知发言人的 speaker-1 返回给业务层；随后同一时序拿到
            # accepted voiceprint 时必须原位升级它，不能因为本次 embedding 有效就绕过该
            # cluster 新建 speaker-2。已确认 voiceprint 的 cluster 在绑定时会清空
            # `_fallback_cluster`，这里再检查 anonymous source 作为双重保护，因此不会覆盖
            # 另一个已确认身份。有效向量同时附着到 fallback，使其之后可参与普通余弦检索。
            cluster = self._fallback_cluster
            self._attach_vector(cluster, vector)
            return cluster
        if vector is None:
            return self._fallback_identity()
        cluster, similarity = self._closest_cluster(vector, anonymous_only=True)
        if cluster is None or similarity < self.cluster_threshold:
            return self._new_cluster(vector)
        self._update_centroid(cluster, vector)
        return cluster

    def _new_cluster(self, vector: list[float] | None) -> _SpeakerCluster:
        """按首次出现顺序创建匿名身份；ID 稳定且不依赖敏感向量内容。"""

        ordinal = self._next_cluster_ordinal
        self._next_cluster_ordinal += 1
        anonymous_name = f"发言人{ordinal}"
        cluster = _SpeakerCluster(
            cluster_id=f"speaker-{ordinal}",
            anonymous_name=anonymous_name,
            centroid=list(vector) if vector is not None else None,
            sample_count=1 if vector is not None else 0,
            speaker_name=anonymous_name,
        )
        if vector is not None:
            self._clusters.append(cluster)
        return cluster

    def _fallback_identity(self) -> _SpeakerCluster:
        """惰性创建并复用尚未绑定 voiceprint 的无 embedding 稳定身份。"""

        if self._fallback_cluster is None:
            self._fallback_cluster = self._new_cluster(None)
        return self._fallback_cluster

    def _attach_vector(self, cluster: _SpeakerCluster, vector: list[float] | None) -> None:
        """把后续有效向量接到 canonical cluster，并纳入普通向量检索。

        fallback cluster 最初可能没有质心，也不会出现在 ``_clusters`` 中。后续同一
        voiceprint 恢复 embedding 时在这里补齐质心，使没有声纹候选的相近片段也能沿用身份。
        """

        if vector is None:
            return
        if cluster.centroid is None or cluster.sample_count <= 0:
            cluster.centroid = list(vector)
            cluster.sample_count = 1
            if cluster not in self._clusters:
                self._clusters.append(cluster)
            return
        if len(cluster.centroid) == len(vector):
            self._update_centroid(cluster, vector)

    @staticmethod
    def _update_centroid(cluster: _SpeakerCluster, vector: list[float]) -> None:
        """使用增量均值更新质心并重新归一化，避免长期会议中的向量幅值漂移。"""

        if cluster.centroid is None or cluster.sample_count <= 0:
            cluster.centroid = list(vector)
            cluster.sample_count = 1
            return
        count = cluster.sample_count
        mean = [
            (old_value * count + new_value) / (count + 1)
            for old_value, new_value in zip(cluster.centroid, vector)
        ]
        # 两个方向相反的有效样本可能均值为零；此时保留旧质心比清空 canonical
        # cluster 更安全，voiceprint 强身份绑定仍然保持不变。
        normalized_mean = _normalize_embedding(mean)
        if normalized_mean is not None:
            cluster.centroid = normalized_mean
        cluster.sample_count = count + 1

    @staticmethod
    def _upgrade_cluster(cluster: _SpeakerCluster, match: Mapping[str, Any]) -> None:
        """把可信库身份绑定到既有 cluster，保留 cluster ID 供历史片段批量修正。"""

        cluster.speaker_name = str(match["speaker_name"])
        cluster.speaker_source = "voiceprint"
        cluster.voiceprint_id = match.get("voiceprint_id")
        cluster.voiceprint_confidence = float(match["confidence"])
        cluster.speaker_title = match.get("speaker_title")

    @staticmethod
    def _public_identity(cluster: _SpeakerCluster) -> SpeakerIdentity:
        """显式挑选公开字段，防止以后给内部 cluster 加字段时意外暴露 embedding。"""

        return SpeakerIdentity(
            speaker_name=cluster.speaker_name,
            speaker_cluster_id=cluster.cluster_id,
            speaker_source=cluster.speaker_source,
            voiceprint_id=cluster.voiceprint_id,
            confidence=(
                cluster.voiceprint_confidence if cluster.speaker_source == "voiceprint" else 0.0
            ),
            speaker_title=cluster.speaker_title,
        )
