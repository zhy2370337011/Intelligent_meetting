"""普通会议系统对接适配。

用户提供的对接文档要求待办保存接口为：
POST /task/management/meeting/taskSave
请求体是任务数组，每个任务包含 taskType、taskName、taskContent、childNodes 等字段。
本模块只负责把智能会议系统内部 TodoItem 映射成该接口需要的字段，
真正 HTTP 推送由 api 或 client 层完成，便于单元测试。
"""

from __future__ import annotations

from typing import Any


DEFAULT_TASK_TYPE = "TaskManagement"


def map_todo_to_task_save_payload(
    todo: dict[str, Any],
    meeting_id: str,
    meeting_name: str,
) -> dict[str, Any]:
    """把 AI 抽取出的待办映射为普通会议系统的任务保存结构。

    Args:
        todo: 内部待办对象，来自大模型抽取或用户编辑。
        meeting_id: 普通会议系统或智能会议系统中的会议 ID。
        meeting_name: 会议名称，用于回填外部任务列表。

    Returns:
        可直接放入 `/task/management/meeting/taskSave` 数组请求体的对象。
    """
    milestones = todo.get("milestones") or []
    child_nodes = [
        {
            "majorTime": item.get("time") or item.get("majorTime") or "",
            "nodeContent": item.get("content") or item.get("nodeContent") or "",
        }
        for item in milestones
    ]

    return {
        "taskType": todo.get("taskType") or DEFAULT_TASK_TYPE,
        "taskName": todo.get("title") or todo.get("taskName") or "未命名待办",
        "taskContent": todo.get("content") or todo.get("taskContent") or "",
        "taskDirection": todo.get("direction") or todo.get("taskDirection") or "",
        "taskAttribute": todo.get("attribute") or todo.get("taskAttribute") or "",
        "responsibleDept": todo.get("ownerDept") or todo.get("responsibleDept") or "",
        "cooperateDept": todo.get("cooperateDept") or "",
        "completeDate": todo.get("dueDate") or todo.get("completeDate") or "",
        "meetingId": meeting_id,
        "meetingName": meeting_name,
        "childNodes": child_nodes,
        "implementMeasures": todo.get("measures") or todo.get("implementMeasures") or "",
        "description": todo.get("description") or "",
        "remark": todo.get("remark") or "",
    }


def build_task_save_request(
    todos: list[dict[str, Any]],
    meeting_id: str,
    meeting_name: str,
) -> list[dict[str, Any]]:
    """批量构造待办推送请求体。

    外部接口要求请求体是数组，因此这里返回 list，接口层只需 JSON 序列化即可。
    """
    return [
        map_todo_to_task_save_payload(todo, meeting_id=meeting_id, meeting_name=meeting_name)
        for todo in todos
    ]

