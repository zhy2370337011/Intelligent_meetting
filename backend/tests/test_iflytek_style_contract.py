import asyncio
import unittest
import zipfile
from io import BytesIO

from fastapi import UploadFile

from app.main import (
    BatchVoiceprintRequest,
    ConfigPatchRequest,
    ManualKeywordRequest,
    MeetingCreateRequest,
    ReplacementRuleRequest,
    SegmentPatchRequest,
    SensitiveRuleRequest,
    TemplateImportRequest,
    VoiceprintGroupRequest,
    VoiceprintRequest,
    app,
    copy_template,
    create_manual_keywords,
    create_replacement_rule,
    create_sensitive_rule,
    create_voiceprint,
    create_voiceprint_group,
    delete_template,
    generate_smart_keywords,
    import_template,
    import_template_file,
    list_manual_keywords,
    list_replacement_rules,
    list_templates,
    list_voiceprint_groups,
    patch_meeting_segment,
    parse_template_file,
    update_sensitive_rule,
    upload_optimization_document,
    extract_document_keywords,
    batch_delete_voiceprints,
    batch_download_voiceprints,
    create_meeting,
    list_meeting_rooms,
)
from app.store import store


class IflytekStyleContractTest(unittest.TestCase):
    """验证“讯飞风全量改造”要求的业务接口都是真实可用的。

    这些测试不是为了复刻竞品 UI，而是把新页面背后的业务边界固定下来：
    模板要区分我的/系统，声纹要有分组和样本状态，识别优化要能保存四类配置，
    会议详情右侧工具栏也必须能调用后端接口并返回稳定结构。
    """

    def setUp(self):
        # 每个用例都重置演示数据库，避免批量删除、模板复制等动作互相污染。
        store.reset()

    def test_templates_support_source_tabs_copy_import_and_system_delete_guard(self):
        """纪要模板页需要“我的模板/系统模板”双 Tab，并且系统模板只能复制不能删除。"""
        all_templates = list_templates(source="all")["items"]
        system_templates = list_templates(source="system")["items"]

        self.assertTrue(any(item["name"] == "企业会议纪要模板" for item in system_templates))
        self.assertTrue(all(item["isSystem"] for item in system_templates))

        copied = copy_template(system_templates[0]["id"])
        self.assertEqual(copied["source"], "my")
        self.assertFalse(copied["isSystem"])
        self.assertIn("复制", copied["name"])

        imported = import_template(
            TemplateImportRequest(
                name="项目复盘纪要模板",
                type="项目复盘",
                sections=["会议信息", "问题复盘", "整改计划"],
                description="从本地 Word 模板导入后的用户模板",
                tags=["项目", "复盘"],
            )
        )
        self.assertEqual(imported["source"], "my")
        self.assertEqual(imported["previewType"], "custom")

        with self.assertRaises(Exception):
            delete_template(system_templates[0]["id"])

    def test_template_file_import_extracts_tags_and_bindings(self):
        """本地模板导入必须真正解析文件、保存模板内容，并生成语音识别后自动填充用的标签绑定。"""
        docx_bytes = BytesIO()
        with zipfile.ZipFile(docx_bytes, "w") as archive:
            # 测试用最小 docx：只写入 Word 的正文 XML，避免引入 python-docx 等额外依赖。
            archive.writestr(
                "word/document.xml",
                """
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body>
                    <w:p><w:r><w:t>会议主题</w:t></w:r></w:p>
                    <w:p><w:r><w:t>会议时间</w:t></w:r></w:p>
                    <w:p><w:r><w:t>会议地点</w:t></w:r></w:p>
                    <w:p><w:r><w:t>主持人</w:t></w:r></w:p>
                    <w:p><w:r><w:t>记录人</w:t></w:r></w:p>
                    <w:p><w:r><w:t>参会人</w:t></w:r></w:p>
                    <w:p><w:r><w:t>会议纪要</w:t></w:r></w:p>
                  </w:body>
                </w:document>
                """,
            )
        docx_bytes.seek(0)

        imported = asyncio.run(
            import_template_file(
                UploadFile(filename="企业会议纪要模板.docx", file=docx_bytes),
                name="本地企业会议纪要模板",
                templateType="企业会议",
                tags="会议主题,会议时间,会议地点,主持人,记录人,参会人,会议纪要",
            )
        )

        self.assertEqual(imported["source"], "my")
        self.assertEqual(imported["previewType"], "imported")
        self.assertEqual(imported["originFilename"], "企业会议纪要模板.docx")
        self.assertIn("会议主题", imported["content"])
        self.assertTrue(any(item["tag"] == "会议纪要" for item in imported["tagBindings"]))
        self.assertTrue(any(item["sourceField"] == "meetingName" for item in imported["tagBindings"]))

    def test_template_file_parse_previews_without_saving(self):
        """识别模板按钮应先真实解析文件并回显标签，但不能直接写入我的模板库。"""
        before_count = len(list_templates(source="my")["items"])
        upload = UploadFile(
            filename="简易纪要模板.txt",
            file=BytesIO("会议主题\n会议时间\n会议地点\n会议纪要\n会议待办".encode("utf-8")),
        )

        parsed = asyncio.run(
            parse_template_file(
                upload,
                name="预览用纪要模板",
                templateType="预览会议",
                tags="",
            )
        )
        after_count = len(list_templates(source="my")["items"])

        self.assertEqual(parsed["name"], "预览用纪要模板")
        self.assertIn("会议纪要", parsed["content"])
        self.assertTrue(any(item["tag"] == "会议待办" for item in parsed["tagBindings"]))
        self.assertEqual(after_count, before_count)

    def test_voiceprint_groups_batch_actions_and_sample_status(self):
        """声纹库管理页需要左侧分组、右侧表格、批量操作和样本上传状态。"""
        group = create_voiceprint_group(VoiceprintGroupRequest(name="党委会发言人", description="重点会议声纹分组"))
        profile = create_voiceprint(
            VoiceprintRequest(
                name="刘主任",
                department="办公室",
                samples=0,
                enabled=True,
                remark="新增发言人",
                groupId=group["id"],
            )
        )

        groups = list_voiceprint_groups()["items"]
        self.assertTrue(any(item["id"] == group["id"] for item in groups))
        self.assertEqual(profile["groupName"], "党委会发言人")
        self.assertEqual(profile["registerStatus"], "pending_sample")

        upload = UploadFile(filename="liuzhuren.wav", file=BytesIO(b"fake voiceprint sample"))
        # 单元测试的字节不是可由 CAM++ 提取真实 embedding 的人声。产品契约要求保留样本
        # 和人员资料，同时诚实进入 waiting_model_config；测试不能再把 mock 响应当成已注册。
        from app.main import upload_voiceprint_sample

        sample_result = asyncio.run(upload_voiceprint_sample(profile["id"], upload))
        # A running real CAM++ rejects these fake bytes as failed; an absent gateway reports waiting.
        # Neither environment may turn a non-audio fixture into a registered production embedding.
        self.assertIn(sample_result["voiceprint"]["registerStatus"], {"waiting_model_config", "failed"})
        expected_model_status = (
            "waiting_model_config"
            if sample_result["voiceprint"]["registerStatus"] == "waiting_model_config"
            else "voiceprint_service_failed"
        )
        self.assertEqual(sample_result["voiceprint"]["modelStatus"], expected_model_status)
        self.assertGreaterEqual(sample_result["voiceprint"]["samples"], 1)

        download = batch_download_voiceprints(BatchVoiceprintRequest(ids=[profile["id"]]))
        self.assertEqual(download["count"], 1)
        self.assertIn(profile["id"], download["items"][0]["id"])

        deleted = batch_delete_voiceprints(BatchVoiceprintRequest(ids=[profile["id"]]))
        self.assertEqual(deleted["deleted"], [profile["id"]])

    def test_optimization_center_supports_four_tabs(self):
        """识别优化中心四个 Tab 都要有后端能力：手动、文档、智能、强制替换。"""
        manual = create_manual_keywords(
            ManualKeywordRequest(language="zh", words=["智能转写", "强制对齐"], enabled=True, applyScope="全部会议")
        )
        self.assertEqual(manual["language"], "zh")
        self.assertIn("智能转写", list_manual_keywords(language="zh")["items"][0]["words"])

        # 文档优化能力应使用可解析的真实输入。伪造 docx 字节理应失败，不能用它证明提取
        # 链路不可用；UTF-8 文本既稳定又覆盖上传、解析、任务落库和关键词提取完整路径。
        document = asyncio.run(
            upload_optimization_document(
                UploadFile(
                    filename="会议材料.txt",
                    file=BytesIO("智能会议 实时转写 声纹识别 会议纪要".encode("utf-8")),
                )
            )
        )
        extracted = extract_document_keywords({"documentId": document["id"]})
        self.assertEqual(extracted["job"]["status"], "completed")
        self.assertGreaterEqual(len(extracted["keywords"]), 1)

        meeting = create_meeting(MeetingCreateRequest(meetingName="智能关键词会议"))
        smart = generate_smart_keywords({"meetingId": meeting["id"], "limit": 6})
        self.assertGreaterEqual(len(smart["keywords"]), 1)

        replacement = create_replacement_rule(
            ReplacementRuleRequest(wrongWord="智能撰写", correctWord="智能转写", enabled=True, applyScope="后续识别")
        )
        self.assertEqual(replacement["correctWord"], "智能转写")
        self.assertTrue(any(item["id"] == replacement["id"] for item in list_replacement_rules()["items"]))

    def test_forbidden_words_support_display_modes_and_case_sensitive(self):
        """禁忌词页面需要“不显示/空格/*”三种方式和英文大小写开关。"""
        rule = create_sensitive_rule(
            SensitiveRuleRequest(
                word="WorldBest",
                replacement="stars",
                displayMode="hide",
                enabled=True,
                scope="展示与导出",
                remark="英文大小写测试",
                caseSensitive=True,
                language="en",
                applyScope="展示",
            )
        )
        self.assertEqual(rule["displayMode"], "hide")
        self.assertTrue(rule["caseSensitive"])
        self.assertEqual(rule["language"], "en")

        updated = update_sensitive_rule(rule["id"], ConfigPatchRequest(displayMode="space", caseSensitive=False))
        self.assertEqual(updated["displayMode"], "space")
        self.assertFalse(updated["caseSensitive"])

    def test_meeting_detail_segment_edit_and_right_tool_actions(self):
        """会议详情中间编辑器和右侧工具栏都必须能落到后端业务接口。"""
        meeting = create_meeting(MeetingCreateRequest(meetingName="详情工具栏联调"))
        completed = store.update_meeting(meeting["id"], {"segments": store._default_segments(meeting["id"]), "processStatus": "completed"})
        segment = completed["segments"][0]

        patched = patch_meeting_segment(
            meeting["id"],
            segment["id"],
            SegmentPatchRequest(text="更新后的转写文本", speakerName="刘主任", marked=True),
        )
        self.assertEqual(patched["text"], "更新后的转写文本")
        self.assertTrue(patched["marked"])

        route_paths = {route.path for route in app.routes}
        self.assertNotIn("/api/meetings/{meeting_id}/tools/mindmap", route_paths)

    def test_removed_modules_no_longer_register_backend_routes(self):
        """看板、日程、写作、AI 工具、知识库已按产品范围从后端路由表删除。"""

        route_paths = {route.path for route in app.routes}
        removed_paths = {
            "/api/board",
            "/api/schedules",
            "/api/knowledge/items",
            "/api/ai-tools",
            "/api/writing/generate",
        }
        self.assertTrue(removed_paths.isdisjoint(route_paths))

        # 会议室列表仍服务于创建会议和系统配置，不随“日程”页面一起删除。
        self.assertGreaterEqual(len(list_meeting_rooms()["items"]), 1)


if __name__ == "__main__":
    unittest.main()
