"""持久化数据仓储。

本模块把智能会议系统从“内存展示”推进到“可重启、可接模型、可查任务”的系统形态。
当前本地默认使用 SQLite，这是为了在 Windows 开发机上无需额外数据库即可直接启动。

正式部署目标已经按用户要求锁定为 KingbaseES：
KingbaseES V008R006C009M001B0014 on aarch64-unknown-linux-gnu, compiled by gcc 7.3.0, 64-bit。
因此本模块刻意把业务方法和 SQL 存储边界分开：上层 API 只调用 Store 方法，不依赖
SQLite 细节；后续接 KingbaseES 时新增 Kingbase 连接适配器即可，不需要改前端和业务路由。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.artifact_service import (
    artifact_envelope,
    artifact_field_for_type,
    draft_artifact_envelope,
    refresh_artifact_states,
    unlinked_artifact_envelope,
)
from app.config import AUDIO_CLIP_DIR, DATABASE_KIND, DATABASE_URL, UPLOAD_DIR, ensure_data_dirs
from app.meeting_domain import bump_transcript_revision
from app.minutes_service import edit_minutes_version, refresh_minutes_version_states


def format_datetime(value: Any | None = None) -> str:
    """把日期统一格式化为 `YYYY-MM-DD HH:mm:ss`。"""
    if value is None:
        date = datetime.now()
    elif isinstance(value, datetime):
        date = value
    elif isinstance(value, (int, float)):
        date = datetime.fromtimestamp(value)
    elif isinstance(value, str):
        if len(value) == 19 and value[4] == "-" and value[13] == ":":
            return value
        normalized = value.replace("/", "-").replace("T", " ")
        try:
            date = datetime.fromisoformat(normalized)
        except ValueError:
            date = datetime.now()
    else:
        date = datetime.now()
    return date.strftime("%Y-%m-%d %H:%M:%S")


def format_date(value: Any | None = None) -> str:
    """返回配置库更新时间使用的日期字符串。"""
    return format_datetime(value).split(" ")[0]


class PersistentStore:
    """智能会议系统持久化仓储。

    表结构采用“业务表 + JSON 文档”折中方案：每类对象都有独立表，便于后续迁移到
    KingbaseES 时建立索引和拆字段；同时完整对象存在 JSON 中，当前迭代能快速覆盖
    前端所需字段，避免把大量展示字段散落到 API 层手工拼接。
    """

    COLLECTION_TABLES = {
        "meetings": "meetings",
        "files": "files",
        "transcripts": "transcripts",
        "voiceprints": "voiceprints",
        "keyword_libraries": "keyword_libraries",
        "sensitive_rules": "sensitive_rules",
        "templates": "minute_templates",
        "highlights": "highlights",
        "jobs": "jobs",
        "integration_status": "integration_status",
        "system_config": "system_config",
        # 讯飞风改造新增集合。这里仍然采用“集合名 -> 表名”的边界，
        # 后续切 KingbaseES 时只需要在 Store 适配层补这些表，不需要改前端或路由。
        "voiceprint_groups": "voiceprint_groups",
        "optimization_documents": "optimization_documents",
        "manual_keywords": "manual_keywords",
        "replacement_rules": "replacement_rules",
        "mindmaps": "mindmaps",
        "schedules": "schedules",
        "meeting_rooms": "meeting_rooms",
        "knowledge_items": "knowledge_items",
        "writing_documents": "writing_documents",
    }

    def __init__(self, db_path: Path | str | None = None, seed_defaults: bool = True) -> None:
        ensure_data_dirs()
        if DATABASE_KIND != "sqlite":
            # 当前运行环境没有 Kingbase Python 驱动；这里明确失败，避免误以为已连接正式库。
            # 后续接 KingbaseES 时在这里注入 Kingbase 连接实现，业务方法保持不变。
            raise RuntimeError("当前代码已预留 KingbaseES 适配边界，但本地运行只内置 SQLite。请先使用 DATABASE_KIND=sqlite。")
        self.db_path = Path(db_path or DATABASE_URL)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        if seed_defaults and not self._has_any("keyword_libraries"):
            self.reset()
        else:
            self._sync_dictionary_aliases()

    @contextmanager
    def _connect(self):
        """创建 SQLite 连接并确保使用完立即关闭。

        sqlite3.Connection 自身作为上下文管理器时只负责事务提交/回滚，不会关闭文件句柄。
        Windows 下测试会立刻删除临时数据库文件，所以这里必须显式 close。
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        """初始化所有业务表。

        KingbaseES 迁移时可把 `payload TEXT` 改为 `payload JSONB`，把 `created_at`
        和 `updated_at` 改为 timestamp，其余业务字段可以逐步拆列加索引。
        """
        with self._connect() as conn:
            for table in self.COLLECTION_TABLES.values():
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
            conn.commit()

    def _has_any(self, collection_name: str) -> bool:
        table = self.COLLECTION_TABLES[collection_name]
        with self._connect() as conn:
            row = conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        return row is not None

    def _save(self, collection_name: str, item: dict[str, Any]) -> dict[str, Any]:
        """保存一个完整业务对象。"""
        table = self.COLLECTION_TABLES[collection_name]
        now = format_datetime()
        item.setdefault("createdAt", now)
        item["updatedAt"] = now
        payload = json.dumps(item, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {table} (id, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (item["id"], payload, item.get("createdAt", now), now),
            )
            conn.commit()
        return item

    def _mutate_meeting_atomic(self, meeting_id: str, mutator: Any) -> tuple[dict[str, Any], Any]:
        """Serialize one meeting read/modify/write operation inside a SQLite write transaction.

        Meeting rows contain the transcript, revision, and derived-version pointers together. A
        Python-level read followed by a later full-row upsert can overwrite a transcript committed
        between those two steps. ``BEGIN IMMEDIATE`` makes every caller of this helper acquire the
        same database write lock *before* reading, so revision checks and the resulting row update
        are one durable operation across threads and processes.

        The mutator must only change the supplied meeting dictionary; it must not call another store
        method that opens a SQLite write connection while this transaction owns the lock.
        """

        table = self.COLLECTION_TABLES["meetings"]
        now = format_datetime()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(f"SELECT payload FROM {table} WHERE id=?", (meeting_id,)).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(meeting_id)
            meeting = json.loads(row["payload"])
            result = mutator(meeting)
            meeting.setdefault("createdAt", now)
            meeting["updatedAt"] = now
            conn.execute(
                f"UPDATE {table} SET payload=?, updated_at=? WHERE id=?",
                (json.dumps(meeting, ensure_ascii=False), now, meeting_id),
            )
            conn.commit()
        return meeting, result

    def backfill_processing_config(
        self,
        meeting_id: str,
        fields: dict[str, Any],
        *,
        guard_key: str,
    ) -> dict[str, Any]:
        """Atomically add a one-time legacy processing snapshot without replacing live transcript data.

        Legacy policy builders run outside the database transaction because they may read several
        configuration collections.  Only their small immutable result is merged here.  The locked
        row is read again before the merge, so a realtime final committed while the snapshot was
        being calculated remains intact.  If another request already populated ``guard_key``, its
        first durable snapshot wins and this caller receives that canonical meeting unchanged.
        """

        def merge_snapshot(meeting: dict[str, Any]) -> bool:
            processing_config = dict(meeting.get("processingConfig") or {})
            if isinstance(processing_config.get(guard_key), dict):
                return False
            # JSON round-tripping prevents later mutation of a caller-owned snapshot from changing
            # the durable meeting object after this transaction has committed.
            for key, value in fields.items():
                processing_config[key] = json.loads(json.dumps(value, ensure_ascii=False))
            meeting["processingConfig"] = processing_config
            return True

        meeting, _ = self._mutate_meeting_atomic(meeting_id, merge_snapshot)
        return meeting

    def attach_artifact_policy_audit(
        self,
        meeting_id: str,
        artifact_field: str,
        audit: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach audit evidence to one artifact while preserving every concurrent meeting field."""

        def attach(meeting: dict[str, Any]) -> dict[str, Any]:
            artifact = meeting.get(artifact_field)
            if not isinstance(artifact, dict):
                return {"sensitivePolicy": json.loads(json.dumps(audit, ensure_ascii=False))}
            artifact["sensitivePolicy"] = json.loads(json.dumps(audit, ensure_ascii=False))
            return artifact

        _, artifact = self._mutate_meeting_atomic(meeting_id, attach)
        return artifact

    def append_export_policy_audit(self, meeting_id: str, audit_record: dict[str, Any]) -> None:
        """Append one export audit under the meeting write lock instead of rewriting a stale row."""

        def append(meeting: dict[str, Any]) -> None:
            meeting.setdefault("exportAudits", []).append(
                json.loads(json.dumps(audit_record, ensure_ascii=False))
            )

        self._mutate_meeting_atomic(meeting_id, append)

    def _get(self, collection_name: str, item_id: str) -> dict[str, Any] | None:
        """读取一个业务对象。"""
        table = self.COLLECTION_TABLES[collection_name]
        with self._connect() as conn:
            row = conn.execute(f"SELECT payload FROM {table} WHERE id=?", (item_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def _list(self, collection_name: str) -> list[dict[str, Any]]:
        """读取一个集合的所有对象。"""
        table = self.COLLECTION_TABLES[collection_name]
        with self._connect() as conn:
            rows = conn.execute(f"SELECT payload FROM {table} ORDER BY created_at DESC").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def _delete(self, collection_name: str, item_id: str) -> bool:
        """删除一个业务对象。"""
        table = self.COLLECTION_TABLES[collection_name]
        with self._connect() as conn:
            cursor = conn.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
            conn.commit()
            return cursor.rowcount > 0

    def _clear_all(self) -> None:
        """清空所有业务表，仅用于测试和重新生成演示数据。"""
        with self._connect() as conn:
            for table in self.COLLECTION_TABLES.values():
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def reset(self) -> None:
        """重置默认演示数据。"""
        self._clear_all()
        for item in self._default_keyword_libraries():
            self._save("keyword_libraries", item)
        for item in self._default_sensitive_rules():
            self._save("sensitive_rules", item)
        for item in self._default_templates():
            self._save("templates", item)
        for item in self._default_voiceprint_groups():
            self._save("voiceprint_groups", item)
        for item in self._default_voiceprints():
            self._save("voiceprints", item)
        for item in self._default_manual_keywords():
            self._save("manual_keywords", item)
        for item in self._default_replacement_rules():
            self._save("replacement_rules", item)
        for item in self._default_meeting_rooms():
            self._save("meeting_rooms", item)
        for item in self._default_knowledge_items():
            self._save("knowledge_items", item)
        self._sync_dictionary_aliases()
        self.create_meeting(
            "2025年12月29日 16:51",
            meeting_id="rec-001",
            keyword_library_ids=["kw-001", "kw-002"],
            created_at="2025-12-29 16:51:00",
            minutes_status="generated",
            process_status="completed",
        )
        self.create_meeting(
            "2025年12月19日 17:31",
            meeting_id="rec-002",
            keyword_library_ids=["kw-001"],
            created_at="2025-12-19 17:31:16",
            minutes_status="ready",
            process_status="completed",
        )
        self.create_meeting(
            "2025年12月18日 15_21区分声纹",
            meeting_id="rec-003",
            keyword_library_ids=["kw-002", "kw-004"],
            created_at="2025-12-18 15:21:43",
            minutes_status="generating",
            process_status="processing",
        )

    @property
    def keyword_libraries(self) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in self._list("keyword_libraries")}

    @property
    def sensitive_rules(self) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in self._list("sensitive_rules")}

    @property
    def templates(self) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in self._list("templates")}

    @property
    def voiceprints(self) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in self._list("voiceprints")}

    @property
    def voiceprint_groups(self) -> dict[str, dict[str, Any]]:
        """返回声纹分组索引，供声纹库管理页左侧分组和人员建档使用。"""
        return {item["id"]: item for item in self._list("voiceprint_groups")}

    @property
    def manual_keywords(self) -> dict[str, dict[str, Any]]:
        """返回手动关键词优化配置，支持中文/英文两类词表。"""
        return {item["id"]: item for item in self._list("manual_keywords")}

    @property
    def replacement_rules(self) -> dict[str, dict[str, Any]]:
        """返回强制替换词组，格式为“错误词 -> 正确词”。"""
        return {item["id"]: item for item in self._list("replacement_rules")}

    @property
    def schedules(self) -> dict[str, dict[str, Any]]:
        """返回预约会议/日程记录。"""
        return {item["id"]: item for item in self._list("schedules")}

    @property
    def meeting_rooms(self) -> dict[str, dict[str, Any]]:
        """返回会议室资源和预定状态。"""
        return {item["id"]: item for item in self._list("meeting_rooms")}

    @property
    def knowledge_items(self) -> dict[str, dict[str, Any]]:
        """返回知识库条目，供 ASR 关键词和 AI 写作引用。"""
        return {item["id"]: item for item in self._list("knowledge_items")}

    @property
    def meetings(self) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in self._list("meetings")}

    @property
    def files(self) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in self._list("files")}

    @files.setter
    def files(self, value: dict[str, dict[str, Any]]) -> None:
        self._replace_collection("files", value.values())

    @property
    def transcripts(self) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in self._list("transcripts")}

    @transcripts.setter
    def transcripts(self, value: dict[str, dict[str, Any]]) -> None:
        self._replace_collection("transcripts", value.values())

    @property
    def highlights(self) -> list[dict[str, Any]]:
        return self._list("highlights")

    def _replace_collection(self, collection_name: str, values: Any) -> None:
        table = self.COLLECTION_TABLES[collection_name]
        with self._connect() as conn:
            conn.execute(f"DELETE FROM {table}")
            conn.commit()
        for item in values:
            self._save(collection_name, item)

    def _sync_dictionary_aliases(self) -> None:
        """兼容旧服务层需要的简单热词/敏感词列表。"""
        self.hotwords = [
            word
            for library in self.keyword_libraries.values()
            if library.get("enabled", True)
            for word in library.get("words", [])
        ]
        self.sensitive_words = [
            rule["word"]
            for rule in self.sensitive_rules.values()
            if rule.get("enabled", True) and rule.get("word")
        ]

    def _default_keyword_libraries(self) -> list[dict[str, Any]]:
        return [
            {"id": "kw-001", "name": "政务会议词库", "words": ["全国政协", "代表委员", "环境生态", "能源结构调整"], "enabled": True, "scope": "重点会议", "updatedAt": "2026-06-24"},
            {"id": "kw-002", "name": "智能会议技术词库", "words": ["Qwen3-ASR", "声纹注册", "强制对齐", "语篇规整"], "enabled": True, "scope": "技术评审", "updatedAt": "2026-06-24"},
            {"id": "kw-003", "name": "项目管理词库", "words": ["里程碑", "责任部门", "协同单位", "待办推送"], "enabled": False, "scope": "项目例会", "updatedAt": "2026-06-20"},
            {"id": "kw-004", "name": "普通会议系统对接词库", "words": ["taskType", "childNodes", "completeDate", "会议归档"], "enabled": True, "scope": "系统对接", "updatedAt": "2026-06-22"},
        ]

    def _default_sensitive_rules(self) -> list[dict[str, Any]]:
        return [
            {"id": "sw-001", "word": "糟糕", "replacement": "stars", "enabled": True, "scope": "展示与导出", "remark": "口语化负面词"},
            {"id": "sw-002", "word": "不合时宜", "replacement": "stars", "enabled": True, "scope": "展示", "remark": "会议展示屏蔽"},
        ]

    def _default_templates(self) -> list[dict[str, Any]]:
        return [
            {"id": "tpl-001", "name": "默认模板", "type": "通用会议", "isDefault": True, "sections": ["会议信息", "会议主题", "会议结论", "待办事项"]},
            {"id": "tpl-002", "name": "重点任务会议模板", "type": "重点任务", "isDefault": False, "sections": ["任务总体方向", "任务内容", "重大时间节点", "责任部门"]},
            {"id": "tpl-003", "name": "党委会纪要模板", "type": "党委会议", "isDefault": False, "sections": ["会议议题", "审议情况", "会议决定", "落实要求"]},
        ]

    def _default_voiceprints(self) -> list[dict[str, Any]]:
        return [
            {"id": "vp-001", "name": "王忠", "speakerName": "王忠", "department": "办公室", "samples": 4, "enabled": True, "lastMatchedAt": "2025-12-19 17:20", "remark": "常用会议发言人"},
            {"id": "vp-002", "name": "薛总", "speakerName": "薛总", "department": "综合部", "samples": 3, "enabled": True, "lastMatchedAt": "2025-12-18 18:40", "remark": "领导发言样本"},
        ]

    def _keyword_library_names(self, ids: list[str]) -> list[str]:
        libraries = self.keyword_libraries
        return [libraries[library_id]["name"] for library_id in ids if library_id in libraries]

    def config_revision(self, collection_name: str) -> str:
        """Return a deterministic version token for the configuration collection snapshot."""

        payload = json.dumps(self._list(collection_name), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def template_snapshot(self, template_id: str) -> dict[str, Any]:
        """Copy the selected template so later edits cannot alter this meeting's history."""

        template = self.templates.get(template_id) or {}
        # JSON round-tripping creates a detached JSON-compatible structure and avoids leaking a
        # mutable record returned by the persistence layer into the meeting's historical snapshot.
        return json.loads(json.dumps(template, ensure_ascii=False))

    def _default_summary(self) -> dict[str, Any]:
        return {
            "keywords": ["能源结构调整", "环境生态的健康指标", "代表委员", "会议纪要", "待办闭环"],
            "topic": "环境生态治理与智能会议系统建设",
            "overview": "会议围绕灰霾治理、能源结构调整、会议记录自动化、待办推送和普通会议系统对接展开讨论。",
            "keyPoints": [
                "代表委员关注控烟和雾霾问题，建议政府在能源结构调整上作出改变。",
                "会议系统需要支持声纹区分、语篇规整、一键纪要和字音同步回听。",
                "待办信息应推送到普通会议系统，形成跨系统闭环跟踪。",
            ],
            "todos": [
                {
                    "title": "完成模型网关联调",
                    "content": "完成 ASR、声纹、纪要模板和普通会议系统待办推送联调。",
                    "ownerDept": "信息中心",
                    "cooperateDept": "办公室",
                    "dueDate": "2026-08-31",
                    "milestones": [{"time": "2026-08-31", "content": "完成 Qwen3-ASR 模型网关接入"}],
                }
            ],
        }

    def _default_minutes(self, template_name: str = "默认模板") -> dict[str, str]:
        return {
            "templateName": template_name,
            "title": "智能会议系统建设会议纪要",
            "content": "会议时间：2025年12月19日 17:31:16\n会议地点：四楼泰山厅\n参会人员：王忠、薛总、陈总\n\n会议主题：环境生态治理与智能会议系统建设\n\n会议结论：持续完善智能会议系统，推进声纹库、关键词库、敏感词库配置。\n\n待办事项：\n1. 信息中心完成模型网关联调。\n2. 办公室补充纪要模板。",
        }

    def _default_segments(self, meeting_id: str) -> list[dict[str, Any]]:
        return [
            {"id": f"{meeting_id}-s1", "speakerName": "王忠", "speakerRole": "办公室", "startMs": 213000, "endMs": 252000, "text": "今天的北京全国两会也进入第四天下午，人民大会堂正在进行的是全国政协会议，第四个发言的委员认为，关注的就是如何应对灰霾。"},
            {"id": f"{meeting_id}-s2", "speakerName": "薛总", "speakerRole": "综合部", "startMs": 230000, "endMs": 270000, "text": "今年以来，我国中东部地区发生了持续大规模灰霾污染事件，污染范围覆盖近270万平方公里，涉及多个重点城市。"},
        ]

    @staticmethod
    def _canonical_string_list(values: Any) -> list[str]:
        """Return trimmed, nonblank, first-occurrence values for durable identifier lists."""

        normalized: list[str] = []
        seen: set[str] = set()
        for value in values or []:
            if not isinstance(value, str):
                continue
            item = value.strip()
            if item and item not in seen:
                normalized.append(item)
                seen.add(item)
        return normalized

    @staticmethod
    def _segments_with_durable_ids(
        meeting_id: str,
        segments: list[dict[str, Any]],
        *,
        existing_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Copy segments and guarantee one unique, nonblank persisted ID per segment.

        ASR providers are allowed to omit IDs or reuse a chunk-local ID. Those values are not
        suitable as source references for revisions, minutes, or todos. We retain the first valid
        occurrence for compatibility, then mint a meeting-scoped ID for every blank/duplicate
        occurrence. Copying also prevents persistence normalization from mutating provider output.
        """

        used = set(PersistentStore._canonical_string_list(existing_ids or []))
        normalized: list[dict[str, Any]] = []
        for source in segments:
            segment = dict(source)
            candidate = str(segment.get("id") or "").strip()
            if not candidate or candidate in used:
                candidate = f"{meeting_id}-seg-{uuid.uuid4().hex[:10]}"
            segment["id"] = candidate
            used.add(candidate)
            normalized.append(segment)
        return normalized

    def create_meeting(
        self,
        name: str,
        location: str = "",
        meeting_id: str | None = None,
        keyword_library_ids: list[str] | None = None,
        template_id: str = "tpl-001",
        language: str = "中文普通话",
        translate_direction: str = "无",
        audio_source: str = "麦克风阵列",
        enable_diarization: bool = True,
        processing_config: dict[str, Any] | None = None,
        created_at: Any | None = None,
        minutes_status: str = "ready",
        process_status: str = "processing",
    ) -> dict[str, Any]:
        mid = meeting_id or f"rec-{uuid.uuid4().hex[:10]}"
        created = format_datetime(created_at)
        # ``None`` preserves the legacy seed/default behavior used by direct store callers. An explicit empty
        # list comes from the quick-meeting UI and must remain empty; using ``or`` here previously enabled two
        # unrelated dictionaries and reduced realtime recognition accuracy through contextual biasing.
        keyword_ids = (
            [library["id"] for library in self.keyword_libraries.values() if library.get("enabled", True)][:2]
            if keyword_library_ids is None
            else self._canonical_string_list(keyword_library_ids)
        )
        # API creation already supplies a normalized immutable snapshot. Reusing its IDs for the
        # legacy top-level response keeps old clients compatible without exposing two conflicting
        # representations of the same meeting configuration.
        if processing_config and "keywordLibraryIds" in processing_config:
            keyword_ids = self._canonical_string_list(processing_config.get("keywordLibraryIds"))
        template = self.templates.get(template_id) or next(iter(self.templates.values()))
        summary = self._default_summary()
        meeting = {
            "id": mid,
            "fileName": name or "未命名会议",
            "meetingName": name or "未命名会议",
            "meetingLocation": location,
            "language": language,
            "translateDirection": translate_direction,
            "audioSource": audio_source,
            "enableDiarization": enable_diarization,
            # A new meeting starts before any durable final segment exists. The empty reason/time
            # fields make that distinction explicit instead of treating meeting creation as a text
            # mutation that would incorrectly invalidate every derived artifact.
            "processingConfig": json.loads(json.dumps(processing_config or {}, ensure_ascii=False)),
            "transcriptRevision": 0,
            "transcriptRevisionReason": "",
            "transcriptRevisionSegmentIds": [],
            "transcriptUpdatedAt": "",
            "keywords": summary["keywords"],
            "keywordLibraryIds": keyword_ids,
            "keywordLibraryNames": self._keyword_library_names(keyword_ids),
            "minutesStatus": minutes_status,
            "processStatus": process_status,
            "createdAt": created,
            "creator": "管理员",
            "status": process_status,
            "segments": self._default_segments(mid) if process_status == "completed" else [],
            "summary": summary,
            "minutes": self._default_minutes(template["name"]),
            # Minutes versions are append-only generation records.  The pointer changes when a
            # user explicitly regenerates, while the processing configuration remains the
            # original meeting binding and is never changed by template switching.
            "minutesVersions": [],
            "minutesCurrentVersionId": None,
            "todos": summary["todos"],
            "decisionItems": ["将待办推送能力纳入普通会议系统接口联调范围。", "会议纪要模板按通用会议、重点任务、党委会三类先行沉淀。"],
            "riskFlags": ["部分发言人声纹样本不足，实时匹配置信度需要提示。", "敏感词规则需区分展示屏蔽和导出屏蔽，避免影响正式归档文本。"],
            "integrationStatus": {"todoPush": "待推送", "minutesArchive": "已归档" if minutes_status == "generated" else "待归档", "transcriptExport": "可导出", "audioReturn": "可回传"},
            "aiToolDrafts": {},
            "files": [],
            "updatedAt": created,
        }
        self._save("meetings", meeting)
        return meeting

    def list_meetings(self, search: str = "", status: str = "all", minutes_status: str = "all", library_id: str = "all", date: str = "") -> list[dict[str, Any]]:
        items = self._list("meetings")
        if search:
            items = [item for item in items if search in item["fileName"] or search in item["creator"] or search in " ".join(item["keywordLibraryNames"])]
        if status != "all":
            items = [item for item in items if item["status"] == status or item["processStatus"] == status]
        if minutes_status != "all":
            items = [item for item in items if item["minutesStatus"] == minutes_status]
        if library_id != "all":
            items = [item for item in items if library_id in item["keywordLibraryIds"]]
        if date:
            items = [item for item in items if item["createdAt"].startswith(date)]
        return sorted(items, key=lambda item: item["createdAt"], reverse=True)

    def get_or_create_meeting(self, meeting_id: str) -> dict[str, Any]:
        """Return an existing meeting or create one only for explicit internal compatibility callers."""

        try:
            return self.get_meeting(meeting_id)
        except KeyError:
            return self.create_meeting(f"Meeting {meeting_id}", meeting_id=meeting_id)

    def get_meeting(self, meeting_id: str) -> dict[str, Any]:
        """Strictly read one meeting while applying safe legacy compatibility refreshes.

        Public HTTP reads use this method so a deleted or mistyped ID remains missing. Historical
        backfills still run for a row that actually exists, but they never synthesize a new meeting.
        """

        meeting = self._get("meetings", meeting_id)
        if not meeting:
            raise KeyError(meeting_id)
        # 早期保存的会议没有 aiToolDrafts 字段。这里按读取路径做一次轻量补齐，
        # 避免前端打开旧导入记录时因为缺少草稿容器而无法保存 AI 工具结果。
        needs_draft_backfill = "aiToolDrafts" not in meeting
        # Records saved before Task 2 may contain an artifact from an older transcript revision.
        # Refresh on read is a compatibility backstop so they cannot appear current merely because
        # their original transcript mutation occurred before the new write-boundary hooks existed.
        artifact_fields = ("summaryArtifact", "minutesArtifact", "todosArtifact", "discourseArtifact", "highlightArtifacts", "minutesVersions", "minutesCurrentVersionId")
        before_refresh = json.dumps({field: meeting.get(field) for field in artifact_fields}, ensure_ascii=False, sort_keys=True)
        refresh_artifact_states(meeting)
        # Task 2 knows one active minutes artifact; Task 6 retains every older generation. Apply
        # the same transcript revision truth to history without removing generated/edit layers.
        refresh_minutes_version_states(meeting)
        after_refresh = json.dumps({field: meeting.get(field) for field in artifact_fields}, ensure_ascii=False, sort_keys=True)
        # Always reconcile the legacy highlight collection, even when the meeting envelope was
        # already stale before this read. Earlier versions could persist a stale meeting artifact
        # beside a current collection row; gating synchronization on a meeting-object change left
        # that historical mismatch permanent after restart.
        if needs_draft_backfill or after_refresh != before_refresh:
            def refresh_locked(current: dict[str, Any]) -> None:
                # Recompute against the row obtained after BEGIN IMMEDIATE.  Applying the detached
                # object calculated above would reintroduce the exact lost-update race this path is
                # meant to repair for historical records.
                current.setdefault("aiToolDrafts", {})
                refresh_artifact_states(current)
                refresh_minutes_version_states(current)

            meeting, _ = self._mutate_meeting_atomic(meeting_id, refresh_locked)
        self._sync_persisted_highlight_artifacts(meeting)
        return meeting

    def _sync_persisted_highlight_artifacts(self, meeting: dict[str, Any]) -> None:
        """Mirror meeting-level highlight state into the legacy ``highlights`` collection.

        Highlights are returned through both the meeting document and a durable collection for
        older callers. The meeting envelope is the revision-aware state updated by transcript
        writers; this method immediately copies that exact envelope into its collection row so a
        consumer of either compatible representation sees the same current, stale, or unlinked
        truth. The shared highlight ID lives inside the legacy generated payload to avoid adding
        a new required field to historic meeting-level envelope responses.
        """

        artifacts_by_highlight_id = {
            str(artifact.get("generatedContent", {}).get("id") or ""): artifact
            for artifact in meeting.get("highlightArtifacts", [])
            if isinstance(artifact, dict)
            and isinstance(artifact.get("generatedContent"), dict)
            and str(artifact.get("generatedContent", {}).get("id") or "")
        }
        if not artifacts_by_highlight_id:
            return

        for item in self.highlights:
            if item.get("meetingId") != meeting.get("id"):
                continue
            expected_artifact = artifacts_by_highlight_id.get(str(item.get("id") or ""))
            if expected_artifact is not None and item.get("artifact") != expected_artifact:
                item["artifact"] = expected_artifact
                self._save("highlights", item)

    @staticmethod
    def _frozen_template_id(meeting: dict[str, Any]) -> str:
        """Read the template selected in the immutable processing snapshot for provenance."""

        processing_config = meeting.get("processingConfig")
        if isinstance(processing_config, dict):
            template_id = str(processing_config.get("templateId") or "").strip()
            if template_id:
                return template_id
        # Older records may predate ``processingConfig``. Retain their legacy value only as a
        # compatibility fallback; newly created meetings always take the frozen branch above.
        return str(meeting.get("templateId") or "").strip()

    def list_minutes_versions(self, meeting_id: str) -> list[dict[str, Any]]:
        """Return ordered historical versions after applying non-destructive stale refreshes.

        The list is stored in generation order rather than sorted at read time.  That order is a
        durable part of the audit trail: the first item is the earliest generation and later
        items show each deliberate template switch or regeneration in the order it occurred.
        """

        meeting = self.get_or_create_meeting(meeting_id)
        return json.loads(json.dumps(meeting.get("minutesVersions", []), ensure_ascii=False))

    def save_minutes_version(self, meeting_id: str, version: dict[str, Any]) -> dict[str, Any]:
        """Append a version and change the compatibility pointer only for a fresh generation.

        Generation has an unavoidable asynchronous window around its model call. The meeting is
        deliberately reloaded here, at the durable write boundary, so a transcript revision that
        landed after generation started cannot be mislabeled as the current result. Late output is
        retained as a stale audit record, but it never changes the pointer or legacy payload.
        """

        version_id = str(version.get("versionId") or "").strip()
        if not version_id:
            raise ValueError("Minutes version requires versionId")
        incoming = json.loads(json.dumps(version, ensure_ascii=False))

        def append_version(meeting: dict[str, Any]) -> dict[str, Any]:
            versions = meeting.setdefault("minutesVersions", [])
            if any(item.get("versionId") == version_id for item in versions if isinstance(item, dict)):
                raise ValueError("Minutes versionId already exists")
            persisted_version = json.loads(json.dumps(incoming, ensure_ascii=False))
            source_revision = int(persisted_version.get("sourceTranscriptRevision", -1))
            current_revision = int(meeting.get("transcriptRevision", 0))
            versions.append(persisted_version)
            if source_revision != current_revision:
                persisted_version["status"] = "stale"
                refresh_minutes_version_states(meeting)
                return persisted_version

            persisted_version["status"] = "current"
            meeting["minutesCurrentVersionId"] = version_id
            refresh_minutes_version_states(meeting)
            meeting["minutesArtifact"] = json.loads(json.dumps(persisted_version, ensure_ascii=False))
            meeting["minutes"] = json.loads(json.dumps(persisted_version["generatedContent"], ensure_ascii=False))
            meeting["minutesStatus"] = "generated"
            meeting["integrationStatus"]["minutesArchive"] = "待归档"
            return persisted_version

        _, persisted_version = self._mutate_meeting_atomic(meeting_id, append_version)
        return json.loads(json.dumps(persisted_version, ensure_ascii=False))

    def set_edited_minutes_version_content(
        self,
        meeting_id: str,
        content: str,
        *,
        version_id: str | None = None,
        legacy_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Update only a selected version's human layer, preserving all generated history.

        Editing a historical version must not move the current pointer or overwrite top-level
        compatibility data for a newer generation.  Editing the pointer target does mirror its
        edited layer into ``minutesArtifact``/``minutes`` so old and new API consumers agree on
        the current visible result.
        """

        meeting = self.get_or_create_meeting(meeting_id)
        current_version_id = str(meeting.get("minutesCurrentVersionId") or "").strip()
        target_version_id = str(version_id or current_version_id).strip()
        if not target_version_id:
            return None
        target = next(
            (item for item in meeting.get("minutesVersions", []) if isinstance(item, dict) and item.get("versionId") == target_version_id),
            None,
        )
        if target is None:
            raise ValueError("Unknown minutes versionId")

        edit_minutes_version(target, content)
        if target_version_id == current_version_id:
            meeting["minutesArtifact"] = json.loads(json.dumps(target, ensure_ascii=False))
            # A legacy minutes client has no edited-layer field. Keep its visible content aligned
            # with the selected current version while the version record retains both layers.
            meeting["minutes"] = legacy_payload or {"content": content}
            meeting["minutesStatus"] = "generated"
            meeting["integrationStatus"]["minutesArchive"] = "待归档"
        self._save("meetings", meeting)
        return json.loads(json.dumps(target, ensure_ascii=False))

    def update_meeting(self, meeting_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        def apply_patch(meeting: dict[str, Any]) -> None:
            for key, value in patch.items():
                if value is not None and key in meeting:
                    # ``keywordLibraryIds`` is a legacy mutable display/filter field. Normalize it
                    # at the write boundary while leaving immutable ``processingConfig`` untouched.
                    meeting[key] = self._canonical_string_list(value) if key == "keywordLibraryIds" else value
            if "keywordLibraryIds" in patch and patch["keywordLibraryIds"] is not None:
                meeting["keywordLibraryNames"] = self._keyword_library_names(meeting["keywordLibraryIds"])

        meeting, _ = self._mutate_meeting_atomic(meeting_id, apply_patch)
        return meeting

    def add_meeting_attachment(self, meeting_id: str, attachment: dict[str, Any]) -> dict[str, Any]:
        """原子追加一个普通会议附件，并返回持久化元数据。"""

        def append_attachment(meeting: dict[str, Any]) -> dict[str, Any]:
            attachments = [item for item in meeting.get("attachments", []) if isinstance(item, dict)]
            # 创建请求可能已经冻结了同名占位元数据；真实上传成功后用带路径和大小的记录替换它。
            attachments = [item for item in attachments if item.get("name") != attachment.get("name")]
            attachments.append(json.loads(json.dumps(attachment, ensure_ascii=False)))
            meeting["attachments"] = attachments
            return attachment

        _meeting, saved = self._mutate_meeting_atomic(meeting_id, append_attachment)
        return saved

    def delete_meeting(self, meeting_id: str) -> bool:
        """Delete one meeting, its owned rows, and its explicitly recorded media files.

        Collection replacement used to rewrite whole tables and left jobs/highlights plus uploaded
        bytes behind. Acquire the write lock before reading ownership, delete only rows whose payload
        names this meeting, then unlink each verified data-root file after the database commit. No
        directory or recursive filesystem operation is used.
        """

        owned_paths: set[Path] = set()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            meeting_row = conn.execute("SELECT payload FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
            if meeting_row is None:
                conn.rollback()
                return False
            meeting = json.loads(meeting_row["payload"])
            for segment in meeting.get("segments", []):
                if segment.get("audioPath"):
                    owned_paths.add(Path(segment["audioPath"]))
            # Older releases duplicated upload metadata inside the meeting document.  Collect that
            # compatibility copy as well as the normalized files table so a partially migrated row
            # cannot leave its exact media file behind after deletion.
            for file_record in meeting.get("files", []):
                if isinstance(file_record, dict) and file_record.get("path"):
                    owned_paths.add(Path(file_record["path"]))

            for collection_name, table in self.COLLECTION_TABLES.items():
                if collection_name == "meetings":
                    continue
                rows = conn.execute(f"SELECT id, payload FROM {table}").fetchall()
                owned_ids: list[str] = []
                for row in rows:
                    payload = json.loads(row["payload"])
                    if payload.get("meetingId") != meeting_id:
                        continue
                    owned_ids.append(str(row["id"]))
                    if collection_name == "files" and payload.get("path"):
                        owned_paths.add(Path(payload["path"]))
                if owned_ids:
                    conn.executemany(f"DELETE FROM {table} WHERE id = ?", [(item_id,) for item_id in owned_ids])
            conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
            conn.commit()

        allowed_roots = [Path(UPLOAD_DIR).resolve(), Path(AUDIO_CLIP_DIR).resolve()]
        for candidate in owned_paths:
            try:
                resolved = candidate.resolve()
                if any(resolved == root or root in resolved.parents for root in allowed_roots) and resolved.is_file():
                    resolved.unlink()
            except OSError:
                # Database deletion is authoritative. A locked media file remains observable to the
                # smoke verifier, which will downgrade cleanup instead of masking the filesystem issue.
                pass
        return True

    def dashboard_overview(self) -> list[dict[str, Any]]:
        today = format_date()
        meetings = self._list("meetings")
        today_count = sum(1 for item in meetings if item["createdAt"].startswith(today))
        ready_minutes = sum(1 for item in meetings if item["minutesStatus"] == "ready")
        pending_todos = sum(len(item.get("todos", [])) for item in meetings)
        voiceprints = self.voiceprints
        enabled_voiceprints = sum(1 for item in voiceprints.values() if item.get("enabled", True))
        total_voiceprints = max(1, len(voiceprints))
        voiceprint_rate = f"{round(enabled_voiceprints / total_voiceprints * 100)}%"
        sensitive_rules = sum(1 for item in self.sensitive_rules.values() if item.get("enabled", True))
        return [
            {"key": "todayMeetings", "label": "今日会议", "value": today_count, "hint": "实时与离线合计"},
            {"key": "readyMinutes", "label": "待生成纪要", "value": ready_minutes, "hint": "可一键成稿"},
            {"key": "pendingTodos", "label": "待推送待办", "value": pending_todos, "hint": "对接普通会议系统"},
            {"key": "voiceprintRate", "label": "声纹匹配率", "value": voiceprint_rate, "hint": "登记人员覆盖"},
            {"key": "sensitiveRules", "label": "敏感词规则", "value": sensitive_rules, "hint": "展示与导出规则"},
        ]

    def create_job(self, meeting_id: str, job_type: str, title: str, steps: list[str]) -> dict[str, Any]:
        job = {"id": f"job-{uuid.uuid4().hex[:10]}", "meetingId": meeting_id, "type": job_type, "title": title, "status": "pending", "currentStep": steps[0] if steps else "pending", "steps": steps, "progress": 0, "message": "", "createdAt": format_datetime()}
        return self._save("jobs", job)

    def update_job(self, job_id: str, status: str | None = None, current_step: str | None = None, progress: int | None = None, message: str | None = None) -> dict[str, Any]:
        job = self.get_job(job_id)
        if status is not None:
            job["status"] = status
        if current_step is not None:
            job["currentStep"] = current_step
        if progress is not None:
            job["progress"] = progress
        if message is not None:
            job["message"] = message
        return self._save("jobs", job)

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self._get("jobs", job_id)
        if not job:
            raise KeyError(job_id)
        return job

    def list_jobs(self, meeting_id: str) -> list[dict[str, Any]]:
        return [job for job in self._list("jobs") if job.get("meetingId") == meeting_id]

    def save_file(self, meeting_id: str, filename: str, path: Path, content_type: str = "") -> dict[str, Any]:
        """Atomically attach one uploaded file and its initial job to an existing meeting.

        The meeting existence check, file row, embedded compatibility copy, and job row share one
        SQLite write transaction.  A delete therefore orders either before this transaction (upload
        receives ``KeyError`` and writes nothing) or after it (delete owns and removes all rows).
        """

        file_id = f"file-{uuid.uuid4().hex[:10]}"
        now = format_datetime()
        record = {"id": file_id, "meetingId": meeting_id, "filename": filename, "fileName": filename, "path": str(path), "contentType": content_type, "createdAt": now, "updatedAt": now, "status": "uploaded", "pipelineStatus": "uploaded", "pipeline": ["uploaded"]}
        steps = ["uploaded", "transcoding", "asr", "voiceprint", "alignment", "minutes", "completed"]
        job = {"id": f"job-{uuid.uuid4().hex[:10]}", "meetingId": meeting_id, "type": "file_pipeline", "title": f"处理 {filename}", "status": "pending", "currentStep": steps[0], "steps": steps, "progress": 0, "message": "", "createdAt": now, "updatedAt": now}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(meeting_id)
            meeting = json.loads(row["payload"])
            meeting.setdefault("files", []).append(record)
            meeting["processStatus"] = "processing"
            meeting["status"] = "processing"
            meeting["updatedAt"] = now
            for table, item in (("files", record), ("jobs", job)):
                conn.execute(
                    f"INSERT INTO {table} (id, payload, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (item["id"], json.dumps(item, ensure_ascii=False), now, now),
                )
            conn.execute(
                "UPDATE meetings SET payload = ?, updated_at = ? WHERE id = ?",
                (json.dumps(meeting, ensure_ascii=False), now, meeting_id),
            )
            conn.commit()
        return record

    def attach_realtime_recording(
        self,
        meeting_id: str,
        filename: str,
        path: Path,
        *,
        duration_ms: int,
        session_token: str,
    ) -> dict[str, Any]:
        """把一场实时会话的录音作为已完成媒体原子挂到会议，不创建导入转写任务。

        普通 ``save_file`` 会启动上传文件流水线，若复用它，实时录音会被误标为待导入文件。
        这里同时写规范化 files 表和 meeting 内兼容副本，但保持实时会议的处理状态不变。
        同一 session token 重试时返回已有记录，避免断线收尾重复附加同一录音。
        """

        now = format_datetime()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(meeting_id)
            meeting = json.loads(row["payload"])
            existing = next(
                (
                    item
                    for item in meeting.get("files", [])
                    if isinstance(item, dict)
                    and item.get("kind") == "realtime_recording"
                    and str(item.get("sessionToken") or "") == str(session_token)
                ),
                None,
            )
            if existing:
                conn.rollback()
                return existing
            record = {
                "id": f"file-{uuid.uuid4().hex[:10]}",
                "meetingId": meeting_id,
                "filename": filename,
                "fileName": filename,
                "path": str(path),
                "contentType": "audio/wav",
                "kind": "realtime_recording",
                "sessionToken": session_token,
                "durationMs": max(0, int(duration_ms)),
                "createdAt": now,
                "updatedAt": now,
                "status": "completed",
                "pipelineStatus": "completed",
                "pipeline": ["recorded", "completed"],
            }
            meeting.setdefault("files", []).append(record)
            meeting["updatedAt"] = now
            conn.execute(
                "INSERT INTO files (id, payload, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (record["id"], json.dumps(record, ensure_ascii=False), now, now),
            )
            conn.execute(
                "UPDATE meetings SET payload = ?, updated_at = ? WHERE id = ?",
                (json.dumps(meeting, ensure_ascii=False), now, meeting_id),
            )
            conn.commit()
        return record

    def add_transcript(self, meeting_id: str, file_id: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
        def append_import(meeting: dict[str, Any]) -> list[dict[str, Any]]:
            processing_config = meeting.get("processingConfig") if isinstance(meeting.get("processingConfig"), dict) else {}
            if processing_config.get("transcriptionMode") != "import":
                raise ValueError("Import transcripts can only be stored on import meetings")
            normalized = self._segments_with_durable_ids(
                meeting_id,
                segments,
                existing_ids=[str(item.get("id") or "") for item in meeting.get("segments", [])],
            )
            meeting["segments"].extend(normalized)
            meeting["processStatus"] = "completed"
            meeting["status"] = "completed"
            meeting["minutesStatus"] = "ready"
            final_ids = [str(item.get("id") or "") for item in normalized if item.get("text")]
            if final_ids:
                bump_transcript_revision(meeting, reason="import_completed", segment_ids=final_ids)
                refresh_artifact_states(meeting)
                refresh_minutes_version_states(meeting)
            return normalized

        meeting, normalized_segments = self._mutate_meeting_atomic(meeting_id, append_import)
        record = {"id": f"tr-{uuid.uuid4().hex[:10]}", "meetingId": meeting_id, "fileId": file_id, "segments": normalized_segments, "createdAt": format_datetime()}
        self._save("transcripts", record)
        self._sync_persisted_highlight_artifacts(meeting)
        file_record = self._get("files", file_id)
        if file_record:
            file_record["pipelineStatus"] = "completed"
            file_record["status"] = "completed"
            file_record["pipeline"] = ["uploaded", "transcoding", "asr", "voiceprint", "alignment", "minutes", "completed"]
            self._save("files", file_record)
        for job in self.list_jobs(meeting_id):
            if job["type"] == "file_pipeline" and job["status"] in {"pending", "running"}:
                self.update_job(job["id"], status="completed", current_step="completed", progress=100)
        return record

    def add_realtime_segment(self, meeting_id: str, segment: dict[str, Any]) -> None:
        if segment.get("isFinal") is False:
            # Partial ASR text is a display preview. Persisting it would let an unstable hypothesis
            # overwrite durable content and would create a false transcript revision, so it stops here.
            return
        def append_final(meeting: dict[str, Any]) -> dict[str, Any]:
            processing_config = meeting.get("processingConfig") if isinstance(meeting.get("processingConfig"), dict) else {}
            if processing_config.get("transcriptionMode") != "realtime":
                raise ValueError("Realtime segments can only be stored on realtime meetings")
            normalized_segment = self._segments_with_durable_ids(
                meeting_id,
                [segment],
                existing_ids=[str(item.get("id") or "") for item in meeting.get("segments", [])],
            )[0]
            meeting["segments"].append(normalized_segment)
            meeting["processStatus"] = "processing"
            meeting["status"] = "processing"
            bump_transcript_revision(meeting, reason="realtime_final", segment_ids=[normalized_segment["id"]])
            refresh_artifact_states(meeting)
            refresh_minutes_version_states(meeting)
            return normalized_segment

        meeting, _ = self._mutate_meeting_atomic(meeting_id, append_final)
        self._sync_persisted_highlight_artifacts(meeting)

    def save_voiceprint(self, registration: dict[str, Any]) -> dict[str, Any]:
        record = {"name": registration.get("speakerName", registration.get("name", "未命名发言人")), "department": registration.get("department", "未分配部门"), "samples": registration.get("samples", 1), "enabled": registration.get("enabled", True), "lastMatchedAt": registration.get("lastMatchedAt", "刚刚"), "remark": registration.get("remark", "通过选中文本注册"), **registration}
        return self._save("voiceprints", record)

    def set_summary(self, meeting_id: str, summary: dict[str, Any]) -> dict[str, Any]:
        meeting = self.get_or_create_meeting(meeting_id)
        meeting["summary"] = summary
        meeting["keywords"] = summary.get("keywords", meeting.get("keywords", []))
        meeting["todos"] = summary.get("todos", [])
        self._save("meetings", meeting)
        return summary

    def set_minutes(self, meeting_id: str, minutes: dict[str, Any]) -> dict[str, Any]:
        meeting = self.get_or_create_meeting(meeting_id)
        meeting["minutes"] = minutes
        meeting["minutesStatus"] = "generated"
        meeting["integrationStatus"]["minutesArchive"] = "待归档"
        self._save("meetings", meeting)
        return minutes

    def save_derived_artifact(
        self,
        meeting_id: str,
        artifact_type: str,
        payload: dict[str, Any],
        source_segment_ids: list[str],
        template_id: str | None = None,
        *,
        generation_transcript_revision: int | None = None,
        sensitive_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a generated artifact and policy audit in one revision-checked transaction.

        Model calls happen outside SQLite and can overlap transcript edits.  The route supplies the
        revision and source IDs captured before generation, then this method compares that immutable
        provenance against the current row inside ``BEGIN IMMEDIATE``.  A late result is retained as
        stale without replacing current artifact/legacy data, transcript segments, or the revision.
        """

        artifact_field = artifact_field_for_type(artifact_type)

        def persist_artifact(meeting: dict[str, Any]) -> dict[str, Any]:
            current_revision = int(meeting.get("transcriptRevision", 0))
            source_revision = current_revision if generation_transcript_revision is None else int(generation_transcript_revision)
            envelope = artifact_envelope(
                meeting,
                payload,
                source_segment_ids=source_segment_ids,
                source_transcript_revision=source_revision,
                template_id=template_id or "",
            )
            if sensitive_policy is not None:
                # Embed the policy audit before commit so a second non-atomic whole-row save cannot
                # restore an earlier transcript snapshot after this artifact was safely persisted.
                envelope["sensitivePolicy"] = json.loads(json.dumps(sensitive_policy, ensure_ascii=False))

            if source_revision != current_revision:
                envelope["status"] = "stale"
                existing = meeting.get(artifact_field)
                existing_is_current = (
                    isinstance(existing, dict)
                    and existing.get("status") == "current"
                    and int(existing.get("sourceTranscriptRevision", -1)) == current_revision
                )
                if existing_is_current:
                    # Singular artifact fields are active UI pointers.  Keep a newer current result
                    # there and retain this obsolete model response separately for audit/review.
                    meeting.setdefault("staleArtifacts", []).append(
                        {"artifactType": artifact_type, **json.loads(json.dumps(envelope, ensure_ascii=False))}
                    )
                else:
                    # With no current result to protect, expose the honest stale envelope through
                    # the compatible singular field so callers can still inspect the late output.
                    meeting[artifact_field] = envelope
                return envelope

            meeting[artifact_field] = envelope
            if artifact_type == "summary":
                meeting["summary"] = payload
                meeting["keywords"] = payload.get("keywords", meeting.get("keywords", []))
                meeting["todos"] = payload.get("todos", meeting.get("todos", []))
            elif artifact_type == "minutes":
                meeting["minutes"] = payload
                meeting["minutesStatus"] = "generated"
                meeting["integrationStatus"]["minutesArchive"] = "待归档"
            elif artifact_type == "todos":
                meeting["todos"] = payload.get("items", payload.get("todos", []))
            elif artifact_type == "discourse":
                meeting["discourse"] = payload
            return envelope

        _, persisted = self._mutate_meeting_atomic(meeting_id, persist_artifact)
        return json.loads(json.dumps(persisted, ensure_ascii=False))

    def set_edited_artifact_content(
        self,
        meeting_id: str,
        artifact_type: str,
        content: str,
        *,
        legacy_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Persist human edits separately so stale transitions cannot erase them.

        A manually edited result must not overwrite ``generatedContent``. That separation preserves
        the model output for audit and preserves the user's text when later transcript changes
        mark the artifact stale; legacy fields still expose the editable content as before.
        """

        meeting = self.get_or_create_meeting(meeting_id)
        artifact_field = artifact_field_for_type(artifact_type)
        artifact = meeting.get(artifact_field)
        if not isinstance(artifact, dict):
            # A draft can be saved before any model call. Do not seed generated content from the
            # legacy minutes/demo payload: it is user-facing compatibility data, not evidence of
            # generation. The separate ``draft`` state remains honest while preserving edit and
            # transcript context for a later stale transition.
            artifact = draft_artifact_envelope(
                meeting,
                source_segment_ids=[str(segment.get("id") or "") for segment in meeting.get("segments", [])],
                template_id=self._frozen_template_id(meeting) if artifact_type == "minutes" else "",
            )
            meeting[artifact_field] = artifact
        artifact["editedContent"] = content
        artifact["editedAt"] = format_datetime()
        if legacy_payload is not None and artifact_type == "minutes":
            meeting["minutes"] = legacy_payload
            meeting["minutesStatus"] = "generated"
            meeting["integrationStatus"]["minutesArchive"] = "待归档"
        self._save("meetings", meeting)
        return artifact

    def add_highlight(self, meeting_id: str, text: str, segment_id: str = "") -> dict[str, Any]:
        """Save a legacy marker plus a provenance envelope for stale-state refreshes."""

        meeting = self.get_or_create_meeting(meeting_id)
        normalized_segment_id = str(segment_id or "").strip()
        persisted_segment_ids = {
            str(segment.get("id") or "").strip()
            for segment in meeting.get("segments", [])
            if str(segment.get("id") or "").strip()
        }
        if normalized_segment_id and normalized_segment_id not in persisted_segment_ids:
            # A nonblank ID is an assertion of canonical transcript provenance. Reject it at the
            # durable boundary as well as the route so direct store callers cannot create a
            # cross-meeting or nonexistent reference that appears navigable and current.
            raise ValueError("Highlight source segment does not belong to this meeting")

        item = {
            "id": f"hl-{uuid.uuid4().hex[:10]}",
            "meetingId": meeting_id,
            "segmentId": normalized_segment_id,
            "text": text,
            "createdAt": format_datetime(),
        }
        payload = {"id": item["id"], "text": text, "segmentId": normalized_segment_id}
        item["artifact"] = (
            artifact_envelope(meeting, payload, source_segment_ids=[normalized_segment_id])
            if normalized_segment_id
            else unlinked_artifact_envelope(meeting, payload)
        )
        # Markers are additive. Keep each envelope so a transcript edit can stale all of them
        # without deleting the historic highlight records returned by the existing endpoint.
        meeting.setdefault("highlightArtifacts", []).append(item["artifact"])
        self._save("meetings", meeting)
        return self._save("highlights", item)

    def save_ai_tool_draft(self, meeting_id: str, tool: str, title: str, content: str) -> dict[str, Any]:
        """保存右侧 AI 工具草稿，供详情页切换工具后直接回显。

        这里不把草稿混进 summary/minutes/todos 正式字段，避免“保存草稿”误触发归档、推送等业务状态。
        用户点击“添加至纪要”时才会走 minutes/draft，把内容写入正式纪要。
        """

        meeting = self.get_or_create_meeting(meeting_id)
        draft = {
            "tool": tool,
            "title": title,
            "content": content,
            "savedAt": format_datetime(),
        }
        meeting.setdefault("aiToolDrafts", {})[tool] = draft
        self._save("meetings", meeting)
        return draft

    def create_config_item(self, collection_name: str, prefix: str, data: dict[str, Any]) -> dict[str, Any]:
        item_id = data.get("id") or f"{prefix}-{uuid.uuid4().hex[:8]}"
        data["id"] = item_id
        if "updatedAt" not in data and collection_name == "keyword_libraries":
            data["updatedAt"] = format_date()
        item = self._save(collection_name, data)
        self._sync_dictionary_aliases()
        return item

    def list_config_items(self, collection_name: str) -> list[dict[str, Any]]:
        return self._list(collection_name)

    def update_config_item(self, collection_name: str, item_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        item = self._get(collection_name, item_id)
        if not item:
            return None
        for key, value in patch.items():
            if value is not None:
                item[key] = value
        if collection_name == "keyword_libraries":
            item["updatedAt"] = format_date()
        saved = self._save(collection_name, item)
        if collection_name == "keyword_libraries":
            for meeting in self._list("meetings"):
                if item_id in meeting["keywordLibraryIds"]:
                    meeting["keywordLibraryNames"] = self._keyword_library_names(meeting["keywordLibraryIds"])
                    self._save("meetings", meeting)
        self._sync_dictionary_aliases()
        return saved

    def delete_config_item(self, collection_name: str, item_id: str) -> bool:
        existed = self._delete(collection_name, item_id)
        self._sync_dictionary_aliases()
        return existed

    def _default_sensitive_rules(self) -> list[dict[str, Any]]:
        """新版禁忌词默认数据。

        这里覆盖文件上方旧的敏感词默认结构，新增 displayMode/caseSensitive/language/applyScope，
        以支撑讯飞式禁忌词页面的“不显示/空格/*”和英文大小写规则。
        """
        return [
            {
                "id": "sw-001",
                "word": "糟糕",
                "replacement": "stars",
                "displayMode": "stars",
                "enabled": True,
                "scope": "展示与导出",
                "remark": "口语化负面词",
                "caseSensitive": False,
                "language": "zh",
                "applyScope": "展示与导出",
            },
            {
                "id": "sw-002",
                "word": "不合时宜",
                "replacement": "hide",
                "displayMode": "hide",
                "enabled": True,
                "scope": "展示",
                "remark": "会议展示屏蔽",
                "caseSensitive": False,
                "language": "zh",
                "applyScope": "展示",
            },
        ]

    def _default_templates(self) -> list[dict[str, Any]]:
        """新版纪要模板默认数据。

        source/isSystem 用于区分“我的模板/系统模板”；系统模板不可删除，只能复制为我的模板。
        previewType 用于前端绘制不同模板预览卡，正式接 Word 模板时可替换为真实预览图或模板文件地址。
        """
        return [
            {
                "id": "tpl-001",
                "name": "默认会议纪要模板",
                "type": "通用会议",
                "isDefault": True,
                "sections": ["会议信息", "会议主题", "会议结论", "待办事项"],
                "source": "my",
                "isSystem": False,
                "previewType": "custom",
                "tags": ["默认", "通用"],
                "description": "用户默认模板，可按本单位纪要格式继续编辑。",
            },
            {
                "id": "tpl-sys-001",
                "name": "企业会议纪要模板",
                "type": "企业会议",
                "isDefault": False,
                "sections": ["会议信息", "议题内容", "会议结论", "行动计划"],
                "source": "system",
                "isSystem": True,
                "previewType": "enterprise",
                "tags": ["系统", "企业"],
                "description": "适合经营例会、项目例会、跨部门协调会。",
            },
            {
                "id": "tpl-sys-002",
                "name": "红头会议纪要模板",
                "type": "红头会议",
                "isDefault": False,
                "sections": ["文件抬头", "会议概况", "审议事项", "落实要求"],
                "source": "system",
                "isSystem": True,
                "previewType": "redhead",
                "tags": ["系统", "正式"],
                "description": "适合正式发文、党委会、专题部署会。",
            },
            {
                "id": "tpl-sys-003",
                "name": "专题会议纪要模板",
                "type": "专题会议",
                "isDefault": False,
                "sections": ["专题背景", "核心观点", "风险问题", "下一步安排"],
                "source": "system",
                "isSystem": True,
                "previewType": "topic",
                "tags": ["系统", "专题"],
                "description": "适合围绕单一议题形成结构化纪要。",
            },
            {
                "id": "tpl-sys-004",
                "name": "通用会议纪要模板",
                "type": "通用会议",
                "isDefault": False,
                "sections": ["会议信息", "会议主题", "会议纪要", "待办事项"],
                "source": "system",
                "isSystem": True,
                "previewType": "general",
                "tags": ["系统", "通用"],
                "description": "适合大多数日常会议的轻量纪要格式。",
            },
        ]

    def _default_voiceprint_groups(self) -> list[dict[str, Any]]:
        """新版声纹分组默认数据，供声纹库管理页左侧分组使用。"""
        return [
            {"id": "vg-all", "name": "全部", "description": "系统虚拟分组，前端用于查看全部声纹。", "isSystem": True},
            {"id": "vg-ungrouped", "name": "未分组", "description": "默认声纹分组。", "isSystem": True},
            {"id": "vg-office", "name": "办公室", "description": "办公室常用会议发言人。", "isSystem": False},
        ]

    def _default_voiceprints(self) -> list[dict[str, Any]]:
        """新版声纹人员默认数据，新增分组、样本文件和模型注册状态。"""
        return [
            {
                "id": "vp-001",
                "name": "王忠",
                "speakerName": "王忠",
                "department": "办公室",
                "samples": 4,
                "enabled": True,
                "lastMatchedAt": "2025-12-19 17:20",
                "remark": "常用会议发言人",
                "groupId": "vg-office",
                "groupName": "办公室",
                "sampleFiles": [],
                "registerStatus": "registered",
                "modelStatus": "mock_registered",
            },
            {
                "id": "vp-002",
                "name": "薛总",
                "speakerName": "薛总",
                "department": "综合部",
                "samples": 3,
                "enabled": True,
                "lastMatchedAt": "2025-12-18 18:40",
                "remark": "领导发言样本",
                "groupId": "vg-ungrouped",
                "groupName": "未分组",
                "sampleFiles": [],
                "registerStatus": "registered",
                "modelStatus": "mock_registered",
            },
        ]

    def _default_manual_keywords(self) -> list[dict[str, Any]]:
        """识别优化中心“关键词手动优化”的默认词表。"""
        return [
            {"id": "mk-001", "language": "zh", "words": ["智能转写", "声纹注册", "强制对齐"], "enabled": True, "applyScope": "全部会议"},
        ]

    def _default_replacement_rules(self) -> list[dict[str, Any]]:
        """识别优化中心“关键词强制替换”的默认规则。"""
        return [
            {"id": "rr-001", "wrongWord": "智能撰写", "correctWord": "智能转写", "enabled": True, "applyScope": "后续识别"},
        ]

    def _default_meeting_rooms(self) -> list[dict[str, Any]]:
        """讯飞式“预定会议室”默认资源。"""
        return [
            {"id": "room-001", "name": "四楼泰山厅", "capacity": 24, "equipment": ["麦克风阵列", "投屏", "录音"], "status": "available", "reservedBy": "", "reservedTime": ""},
            {"id": "room-002", "name": "二楼黄山厅", "capacity": 12, "equipment": ["投屏", "视频会议"], "status": "available", "reservedBy": "", "reservedTime": ""},
            {"id": "room-003", "name": "一楼洽谈室", "capacity": 6, "equipment": ["白板"], "status": "maintenance", "reservedBy": "", "reservedTime": ""},
        ]

    def _default_knowledge_items(self) -> list[dict[str, Any]]:
        """讯飞式知识库默认条目，和关键词优化、AI 写作共享语料。"""
        return [
            {"id": "kb-001", "title": "智能会议建设规范", "category": "制度", "keywords": ["智能转写", "声纹", "纪要"], "content": "会议系统需支持实时转写、声纹区分、纪要生成和任务闭环。", "updatedAt": format_datetime()},
            {"id": "kb-002", "title": "普通会议系统对接说明", "category": "接口", "keywords": ["taskSave", "待办推送"], "content": "待办通过 taskSave 接口推送，需包含责任部门、完成时间和子节点。", "updatedAt": format_datetime()},
        ]

    def create_schedule(self, data: dict[str, Any]) -> dict[str, Any]:
        """创建预约会议/日程记录。"""
        item = {
            "id": data.get("id") or f"sch-{uuid.uuid4().hex[:8]}",
            "meetingName": data.get("meetingName") or "未命名预约会议",
            "startTime": data.get("startTime") or format_datetime(),
            "durationMinutes": int(data.get("durationMinutes") or 60),
            "roomId": data.get("roomId") or "",
            "roomName": data.get("roomName") or "",
            "participants": data.get("participants") or [],
            "agenda": data.get("agenda") or "",
            "status": data.get("status") or "scheduled",
        }
        return self._save("schedules", item)

    def reserve_room(self, room_id: str, meeting_name: str, reserved_time: str, reserved_by: str = "管理员") -> dict[str, Any] | None:
        """预定会议室，并把会议名写入会议室状态。"""
        room = self._get("meeting_rooms", room_id)
        if not room:
            return None
        room["status"] = "reserved"
        room["reservedBy"] = reserved_by
        room["reservedTime"] = reserved_time
        room["reservedMeeting"] = meeting_name
        return self._save("meeting_rooms", room)

    def create_knowledge_item(self, data: dict[str, Any]) -> dict[str, Any]:
        """保存知识库条目，供关键词和 AI 写作复用。"""
        item = {
            "id": data.get("id") or f"kb-{uuid.uuid4().hex[:8]}",
            "title": data.get("title") or "未命名知识",
            "category": data.get("category") or "通用",
            "keywords": data.get("keywords") or [],
            "content": data.get("content") or "",
            "updatedAt": format_datetime(),
        }
        return self._save("knowledge_items", item)

    def save_writing_document(self, data: dict[str, Any]) -> dict[str, Any]:
        """保存 AI 写作生成结果。"""
        item = {
            "id": data.get("id") or f"doc-{uuid.uuid4().hex[:8]}",
            "title": data.get("title") or "AI 写作稿",
            "scene": data.get("scene") or "会议通知",
            "prompt": data.get("prompt") or "",
            "content": data.get("content") or "",
            "createdAt": format_datetime(),
        }
        return self._save("writing_documents", item)

    def list_templates(self, source: str = "all") -> list[dict[str, Any]]:
        """按来源筛选模板，source=my/system/all。"""
        templates = self._list("templates")
        if source == "all":
            return templates
        return [item for item in templates if item.get("source", "my") == source]

    def copy_template(self, template_id: str) -> dict[str, Any] | None:
        """把系统模板复制到我的模板。

        系统模板属于内置资产，不能直接编辑或删除；复制后生成用户模板，后续可自由修改。
        """
        template = self._get("templates", template_id)
        if not template:
            return None
        copied = dict(template)
        copied["id"] = f"tpl-{uuid.uuid4().hex[:8]}"
        copied["name"] = f"{template.get('name', '模板')} 复制"
        copied["source"] = "my"
        copied["isSystem"] = False
        copied["isDefault"] = False
        copied["previewType"] = template.get("previewType", "custom")
        return self._save("templates", copied)

    def _resolve_voiceprint_group(self, group_id: str | None) -> tuple[str, str]:
        """根据 groupId 得到分组名称；未知分组统一落到“未分组”。"""
        groups = self.voiceprint_groups
        normalized_id = group_id if group_id in groups and group_id != "vg-all" else "vg-ungrouped"
        return normalized_id, groups.get(normalized_id, {"name": "未分组"}).get("name", "未分组")

    def save_voiceprint(self, registration: dict[str, Any]) -> dict[str, Any]:
        """保存声纹人员或声纹注册结果。

        该方法覆盖旧实现，保证所有声纹记录都具备分组、样本明细、注册状态和模型状态字段。
        真正接 CAM++ 时，modelStatus 可记录 embedding 写入结果，sampleFiles 可记录原始样本文件。
        """
        group_id, group_name = self._resolve_voiceprint_group(registration.get("groupId"))
        record = {
            "id": registration.get("id") or f"vp-{uuid.uuid4().hex[:8]}",
            "name": registration.get("speakerName", registration.get("name", "未命名发言人")),
            "speakerName": registration.get("speakerName", registration.get("name", "未命名发言人")),
            "department": registration.get("department", "未分配部门"),
            "samples": registration.get("samples", 1),
            "enabled": registration.get("enabled", True),
            "lastMatchedAt": registration.get("lastMatchedAt", "刚刚"),
            "remark": registration.get("remark", "通过选中文本注册"),
            "groupId": group_id,
            "groupName": group_name,
            "sampleFiles": registration.get("sampleFiles", []),
            "registerStatus": registration.get("registerStatus", "pending_sample"),
            "modelStatus": registration.get("modelStatus", "waiting_sample"),
            **registration,
        }
        record["groupId"], record["groupName"] = self._resolve_voiceprint_group(record.get("groupId"))
        return self._save("voiceprints", record)

    def update_meeting_segment(self, meeting_id: str, segment_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        """更新会议详情页中单条转写片段。

        前端编辑器修改文本、发言人或重点标记时都会走这里；如果后续把 segments 拆成独立表，
        仍可以保持这个方法签名不变。
        """
        def apply_patch(meeting: dict[str, Any]) -> dict[str, Any] | None:
            updated_segment = None
            transcript_changed = False
            for segment in meeting.get("segments", []):
                if segment.get("id") == segment_id:
                    for key, value in patch.items():
                        if value is not None:
                            if key in {"text", "speakerName"} and segment.get(key) != value:
                                transcript_changed = True
                            segment[key] = value
                    updated_segment = segment
                    break
            if updated_segment is not None and transcript_changed:
                bump_transcript_revision(meeting, reason="segment_patch", segment_ids=[segment_id])
                refresh_artifact_states(meeting)
                refresh_minutes_version_states(meeting)
            return updated_segment

        meeting, updated_segment = self._mutate_meeting_atomic(meeting_id, apply_patch)
        if updated_segment is not None:
            self._sync_persisted_highlight_artifacts(meeting)
        return updated_segment

    def update_meeting_segments_batch(
        self,
        meeting_id: str,
        *,
        expected_revision: int,
        updates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """使用乐观锁原子保存多个逐字稿片段，并且只递增一次 transcript revision。

        浏览器把连续同一发言人的多个 segment 放进一个视觉卡片，但持久层不能真的拼接或删除
        segment，否则音频定位、AI source range 和原始 ASR 审计都会失去稳定锚点。本方法先在
        内存副本中完成所有校验，确认 revision 和每个 id 都有效后才修改会议对象。
        """

        normalized_updates: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for update in updates:
            segment_id = str(update.get("segmentId") or "").strip()
            if not segment_id:
                raise ValueError("segmentId 不能为空")
            if segment_id in seen_ids:
                raise ValueError(f"重复的转写片段：{segment_id}")
            seen_ids.add(segment_id)
            normalized_updates.append(
                {
                    "segmentId": segment_id,
                    "text": update.get("text"),
                    "speakerName": update.get("speakerName"),
                }
            )

        def apply_batch(meeting: dict[str, Any]) -> list[str]:
            current_revision = int(meeting.get("transcriptRevision", 0))
            if current_revision != int(expected_revision):
                raise ValueError(
                    f"Transcript revision conflict: expected {expected_revision}, current {current_revision}"
                )

            segments_by_id = {
                str(segment.get("id") or ""): segment
                for segment in meeting.get("segments", [])
                if str(segment.get("id") or "")
            }
            missing_ids = [item["segmentId"] for item in normalized_updates if item["segmentId"] not in segments_by_id]
            if missing_ids:
                # 在写入任何字段前拒绝整批请求，保证缺失一个 id 时其余修改也不会落库。
                raise KeyError(",".join(missing_ids))

            changed_ids: list[str] = []
            for update in normalized_updates:
                segment = segments_by_id[update["segmentId"]]
                changed = False
                for key in ("text", "speakerName"):
                    value = update.get(key)
                    if value is not None and segment.get(key) != value:
                        segment[key] = value
                        changed = True
                if changed:
                    changed_ids.append(update["segmentId"])

            if changed_ids:
                bump_transcript_revision(meeting, reason="segment_batch_patch", segment_ids=changed_ids)
                refresh_artifact_states(meeting)
                refresh_minutes_version_states(meeting)
            return changed_ids

        meeting, changed_ids = self._mutate_meeting_atomic(meeting_id, apply_batch)
        if changed_ids:
            self._sync_persisted_highlight_artifacts(meeting)
        return {
            "meetingId": meeting_id,
            "transcriptRevision": int(meeting.get("transcriptRevision", 0)),
            "updatedSegmentIds": changed_ids,
            "segments": meeting.get("segments", []),
        }

    def apply_sensitive_policy_snapshot(self, meeting_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        """为一场既有会议切换禁忌词派生策略，但绝不修改原始逐字稿或 transcript revision。"""

        def apply_policy(meeting: dict[str, Any]) -> str:
            processing_config = meeting.get("processingConfig") if isinstance(meeting.get("processingConfig"), dict) else {}
            previous = processing_config.get("sensitivePolicy")
            history = meeting.setdefault("sensitivePolicyVersions", [])
            if isinstance(previous, dict) and previous.get("ruleVersion"):
                history.append(json.loads(json.dumps(previous, ensure_ascii=False)))
            processing_config["sensitivePolicy"] = json.loads(json.dumps(snapshot, ensure_ascii=False))
            processing_config["sensitiveRuleVersion"] = str(snapshot.get("ruleVersion") or "")
            processing_config["sensitivePolicyFrozenAt"] = "explicit_revision"
            meeting["processingConfig"] = processing_config
            # AI 结果是按旧策略输入生成的，必须明确过期；展示和导出会在下一次读取时直接按新快照派生。
            for field in ("summaryArtifact", "minutesArtifact", "todosArtifact", "discourseArtifact", "speakerSummaryArtifact"):
                artifact = meeting.get(field)
                if isinstance(artifact, dict):
                    artifact["status"] = "stale"
                    artifact["staleReason"] = "sensitive_policy_changed"
            for version in meeting.get("minutesVersions") or []:
                if isinstance(version, dict):
                    version["status"] = "stale"
                    version["staleReason"] = "sensitive_policy_changed"
            return str(snapshot.get("ruleVersion") or "")

        meeting, rule_version = self._mutate_meeting_atomic(meeting_id, apply_policy)
        return {
            "meetingId": meeting_id,
            "sensitiveRuleVersion": rule_version,
            "transcriptRevision": int(meeting.get("transcriptRevision", 0)),
            "rawTranscriptChanged": False,
        }

    def replace_transcript_from_retranscription(
        self,
        meeting_id: str,
        file_id: str,
        segments: list[dict[str, Any]],
        recognition_policy: dict[str, Any],
    ) -> dict[str, Any]:
        """成功识别后原子切换整份逐字稿；失败调用方不会进入本方法，因此当前版本始终安全。"""

        def replace(meeting: dict[str, Any]) -> list[dict[str, Any]]:
            old_segments = json.loads(json.dumps(meeting.get("segments") or [], ensure_ascii=False))
            old_revision = int(meeting.get("transcriptRevision", 0))
            meeting.setdefault("transcriptVersions", []).append(
                {
                    "versionId": f"tv-{uuid.uuid4().hex[:10]}",
                    "transcriptRevision": old_revision,
                    "segments": old_segments,
                    "createdAt": format_datetime(),
                    "reason": "before_retranscription",
                }
            )
            normalized = self._segments_with_durable_ids(meeting_id, segments, existing_ids=[])
            meeting["segments"] = normalized
            processing_config = meeting.get("processingConfig") if isinstance(meeting.get("processingConfig"), dict) else {}
            processing_config["recognitionPolicy"] = json.loads(json.dumps(recognition_policy, ensure_ascii=False))
            processing_config["recognitionPolicy"]["frozenAt"] = "explicit_retranscription"
            meeting["processingConfig"] = processing_config
            changed_ids = [str(item.get("id") or "") for item in normalized if item.get("text")]
            # 即使新结果为空也不能覆盖旧结果；路由会在进入事务前拒绝空识别结果。
            bump_transcript_revision(meeting, reason="retranscription_switch", segment_ids=changed_ids)
            refresh_artifact_states(meeting)
            refresh_minutes_version_states(meeting)
            meeting["processStatus"] = "completed"
            meeting["status"] = "completed"
            meeting["minutesStatus"] = "ready"
            return normalized

        meeting, normalized_segments = self._mutate_meeting_atomic(meeting_id, replace)
        record = {
            "id": f"tr-{uuid.uuid4().hex[:10]}",
            "meetingId": meeting_id,
            "fileId": file_id,
            "segments": normalized_segments,
            "createdAt": format_datetime(),
            "type": "retranscription",
        }
        self._save("transcripts", record)
        self._sync_persisted_highlight_artifacts(meeting)
        return {
            "meetingId": meeting_id,
            "transcriptId": record["id"],
            "transcriptRevision": int(meeting.get("transcriptRevision", 0)),
            "segments": normalized_segments,
            "transcriptVersions": meeting.get("transcriptVersions", []),
        }

    def update_meeting_todo(self, meeting_id: str, todo_index: int, patch: dict[str, Any]) -> dict[str, Any]:
        """原子保存一条会后待办的人工字段，来源范围不允许被普通编辑请求覆盖。"""

        editable_fields = {"title", "content", "owner", "ownerDept", "deadline", "dueDate", "status"}

        def update_todo(meeting: dict[str, Any]) -> dict[str, Any]:
            todos = meeting.get("todos") if isinstance(meeting.get("todos"), list) else []
            if todo_index < 0 or todo_index >= len(todos) or not isinstance(todos[todo_index], dict):
                raise KeyError(str(todo_index))
            for key, value in patch.items():
                if key in editable_fields and value is not None:
                    todos[todo_index][key] = value
            todos[todo_index]["updatedAt"] = format_datetime()
            meeting["todos"] = todos
            return todos[todo_index]

        _, item = self._mutate_meeting_atomic(meeting_id, update_todo)
        return item

    def update_realtime_speaker_identity(
        self,
        meeting_id: str,
        *,
        segment_id: str,
        cluster_id: str,
        session_token: str,
        patch: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """原子升级一个实时说话人簇的公开身份字段。

        声纹与 embedding 在 ASR 最终文本之后异步完成，结果到达时“最后一段”可能已经变成
        另一句话。因此更新入口必须使用稳定 ``segment_id``，并且只有同一 ``cluster_id`` 的
        历史片段可以随 canonical 身份一起升级。正文、时间戳和原始向量永远不在白名单内。
        """

        if not str(session_token or "").strip():
            raise ValueError("realtime speaker identity update requires a session token")
        allowed_fields = {
            "speakerName",
            "speakerTitle",
            "speakerClusterId",
            "speakerSource",
            "voiceprintId",
            "voiceprintConfidence",
        }
        safe_patch = {
            key: value
            for key, value in patch.items()
            if key in allowed_fields and value is not None
        }
        if not safe_patch:
            return []

        def apply_identity(meeting: dict[str, Any]) -> list[dict[str, Any]]:
            affected: list[dict[str, Any]] = []
            for segment in meeting.get("segments", []):
                # speaker-1 只在一次 WebSocket 会话内唯一。暂停后重新开始会再次从 speaker-1
                # 编号，因此同簇批量升级还必须受 session token 限制，不能改到新会话的同名簇。
                same_session = str(segment.get("realtimeSessionToken") or "") == str(session_token)
                if not same_session:
                    continue
                same_exact_segment = str(segment.get("id") or "") == str(segment_id)
                same_existing_cluster = bool(cluster_id) and str(segment.get("speakerClusterId") or "") == str(cluster_id)
                if not (same_exact_segment or same_existing_cluster):
                    continue
                segment.update(safe_patch)
                affected.append(segment)
            if affected:
                # 发言人姓名会影响纪要归属和待办责任人，因此它与手工改名一样产生一次新的
                # transcript revision。一次簇升级只 bump 一次，避免多段历史记录制造版本风暴。
                bump_transcript_revision(
                    meeting,
                    reason="realtime_speaker_identity",
                    segment_ids=[str(item.get("id") or "") for item in affected],
                )
                refresh_artifact_states(meeting)
                refresh_minutes_version_states(meeting)
            return affected

        meeting, affected = self._mutate_meeting_atomic(meeting_id, apply_identity)
        if affected:
            self._sync_persisted_highlight_artifacts(meeting)
        return affected

    def apply_realtime_diarization(
        self,
        meeting_id: str,
        *,
        session_token: str,
        speaker_patches: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按整场分离结果原子修正一次实时会话的所有发言人字段。

        3D-Speaker 先看完整时间线再聚类，结果比逐短句阈值稳定。所有 patch 在同一 SQLite
        事务中应用并只增加一次 transcript revision；任何非法 segment 都不会造成部分提交。
        人工改名和可信声纹结果不被匿名分离覆盖，避免会后分离抹掉用户已经确认的身份。
        """

        if not str(session_token or "").strip():
            raise ValueError("realtime diarization requires a session token")
        allowed_fields = {
            "speakerName",
            "speakerTitle",
            "speakerClusterId",
            "speakerSource",
            "voiceprintId",
            "voiceprintConfidence",
        }
        normalized_patches = {
            str(segment_id): {
                key: value
                for key, value in patch.items()
                if key in allowed_fields and value is not None
            }
            for segment_id, patch in speaker_patches.items()
            if str(segment_id).strip() and isinstance(patch, dict)
        }

        def apply_all(meeting: dict[str, Any]) -> list[dict[str, Any]]:
            affected: list[dict[str, Any]] = []
            for segment in meeting.get("segments", []):
                if str(segment.get("realtimeSessionToken") or "") != str(session_token):
                    continue
                patch = normalized_patches.get(str(segment.get("id") or ""))
                if not patch:
                    continue
                if str(segment.get("speakerSource") or "") in {"manual", "voiceprint"}:
                    continue
                changed = any(segment.get(key) != value for key, value in patch.items())
                if not changed:
                    continue
                segment.update(patch)
                affected.append(segment)
            if affected:
                bump_transcript_revision(
                    meeting,
                    reason="realtime_diarization",
                    segment_ids=[str(item.get("id") or "") for item in affected],
                )
                refresh_artifact_states(meeting)
                refresh_minutes_version_states(meeting)
            return affected

        meeting, affected = self._mutate_meeting_atomic(meeting_id, apply_all)
        if affected:
            self._sync_persisted_highlight_artifacts(meeting)
        return affected

    def rename_meeting_speaker(self, meeting_id: str, previous_name: str, new_name: str) -> list[dict[str, Any]]:
        """Rename one speaker across persisted segments and return the affected records."""

        def rename(meeting: dict[str, Any]) -> list[dict[str, Any]]:
            changed_segments = []
            for segment in meeting.get("segments", []):
                if segment.get("speakerName") == previous_name and new_name and previous_name != new_name:
                    segment["speakerName"] = new_name
                    changed_segments.append(segment)
            if changed_segments:
                bump_transcript_revision(
                    meeting,
                    reason="speaker_rename",
                    segment_ids=[str(segment.get("id") or "") for segment in changed_segments],
                )
                refresh_artifact_states(meeting)
                refresh_minutes_version_states(meeting)
            return changed_segments

        meeting, changed_segments = self._mutate_meeting_atomic(meeting_id, rename)
        if changed_segments:
            self._sync_persisted_highlight_artifacts(meeting)
        return changed_segments

    def save_mindmap(self, meeting_id: str, mindmap: dict[str, Any]) -> dict[str, Any]:
        """保存会议导图结果，供详情页右侧“导图”工具复用。"""
        mindmap.setdefault("id", f"mm-{uuid.uuid4().hex[:8]}")
        mindmap["meetingId"] = meeting_id
        return self._save("mindmaps", mindmap)

    def delete_config_item(self, collection_name: str, item_id: str) -> bool:
        """删除配置项。

        这里覆盖旧删除方法：系统模板是内置模板资产，不允许直接删除，只能复制为我的模板后再编辑。
        """
        if collection_name == "templates":
            item = self._get(collection_name, item_id)
            if item and item.get("isSystem"):
                raise ValueError("系统模板不可删除，请先复制到我的模板")
        existed = self._delete(collection_name, item_id)
        self._sync_dictionary_aliases()
        return existed


store = PersistentStore()
