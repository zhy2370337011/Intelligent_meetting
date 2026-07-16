"""运行配置。

配置全部从环境变量读取，避免把模型服务地址、外部会议系统地址或登录信息写死。
首版默认使用 mock 模式，保证没有 910B 模型服务时也能完整联调页面。
"""

from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _load_local_env() -> None:
    """加载 backend/.env 中的本地配置。

    当前项目后端依赖尽量轻量，没有强制引入 python-dotenv。这里实现一个足够
    明确的 `.env` 解析器：只处理 `KEY=value`，忽略空行和注释，不覆盖已经由
    外部进程注入的环境变量。这样既能在本地直接写入 DashScope Key 和智能体
    平台配置，也能在服务器上继续使用系统环境变量覆盖。
    """

    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_local_env()
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
NORMALIZED_DIR = DATA_DIR / "normalized"
EXPORT_DIR = DATA_DIR / "exports"
AUDIO_CLIP_DIR = DATA_DIR / "audio_clips"

# 本地开发默认使用 SQLite 文件，便于 Windows 上直接启动。
# 正式政企部署目标为 KingbaseES V008R006C009M001B0014 on aarch64-unknown-linux-gnu。
# 后续切换 KingbaseES 时使用 DATABASE_KIND=kingbase，并由 Kingbase 适配器读取 DATABASE_URL；
# API 层和前端不需要感知底层数据库变化。
DATABASE_KIND = os.getenv("DATABASE_KIND", "sqlite")
DATABASE_URL = os.getenv("DATABASE_URL", str(DATA_DIR / "meeting_system.db"))

ASR_GATEWAY_MODE = os.getenv("ASR_GATEWAY_MODE", "mock")
ASR_GATEWAY_BASE_URL = os.getenv("ASR_GATEWAY_BASE_URL", "")
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com")
DASHSCOPE_SYNC_MODEL = os.getenv("DASHSCOPE_SYNC_MODEL", "qwen3-asr-flash")
DASHSCOPE_FILETRANS_MODEL = os.getenv("DASHSCOPE_FILETRANS_MODEL", "qwen3-asr-flash-filetrans")
MODEL_MOCK_MODE = os.getenv("MODEL_MOCK_MODE", "true").lower() in {"1", "true", "yes", "on"}
ALIGNMENT_GATEWAY_BASE_URL = os.getenv("ALIGNMENT_GATEWAY_BASE_URL", "")
VOICEPRINT_GATEWAY_BASE_URL = os.getenv("VOICEPRINT_GATEWAY_BASE_URL", "")
VAD_GATEWAY_BASE_URL = os.getenv("VAD_GATEWAY_BASE_URL", "")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

MEETING_SYSTEM_BASE_URL = os.getenv("MEETING_SYSTEM_BASE_URL", "")
MEETING_SYSTEM_TOKEN = os.getenv("MEETING_SYSTEM_TOKEN", "")

# DeepSeek 大模型配置。
# 说明：用户已经明确要求“后端自己编排会议能力，不再连接智能体平台”。因此摘要、纪要、待办、
# 翻译、语篇规整五个能力会先在本地 Python 中整理输入/兜底结构，再统一调用 DeepSeek 的
# OpenAI 兼容 Chat Completions 接口生成结构化 JSON。这里不把密钥写入源码，只从 backend/.env
# 或系统环境变量读取，便于本机联调和后续服务器 Secret 覆盖。
AI_PROVIDER = os.getenv("AI_PROVIDER", "deepseek").lower()
AI_MOCK_MODE = os.getenv("AI_MOCK_MODE", "").lower() in {"1", "true", "yes", "on"}
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
DEEPSEEK_TIMEOUT_SECONDS = int(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "120"))

# 下面保留旧智能体平台变量只是为了兼容历史 .env 和文档迁移，不再参与五个会议 AI 能力调用。
# 如果后续还有单独页面需要查看旧配置，可重新做独立适配；当前智慧会议主链路只走本地编排+DeepSeek。
WORKFLOW_INVOKE_URL = os.getenv("WORKFLOW_INVOKE_URL", "")
LLM_WORKFLOW_MODE = os.getenv("LLM_WORKFLOW_MODE", "mock").lower()
WORKFLOW_MEETING_SUMMARY_ID = os.getenv("WORKFLOW_MEETING_SUMMARY_ID", "")
# 下面几个工作流 ID 同时兼容“代码短名”和“业务语义长名”。
# 早期代码里使用 WORKFLOW_MINUTES_ID / WORKFLOW_TODO_ID / WORKFLOW_DISCOURSE_ID；
# 后续部署人员更容易按功能理解 WORKFLOW_MEETING_MINUTES_ID、
# WORKFLOW_TODO_EXTRACT_ID、WORKFLOW_DISCOURSE_REWRITE_ID。
# 这里做别名兜底，避免因为环境变量名字不一致导致系统悄悄退回 mock。
WORKFLOW_MINUTES_ID = os.getenv("WORKFLOW_MINUTES_ID") or os.getenv("WORKFLOW_MEETING_MINUTES_ID", "")
WORKFLOW_TODO_ID = os.getenv("WORKFLOW_TODO_ID") or os.getenv("WORKFLOW_TODO_EXTRACT_ID", "")
WORKFLOW_TRANSLATE_ID = os.getenv("WORKFLOW_TRANSLATE_ID", "")
WORKFLOW_DISCOURSE_ID = os.getenv("WORKFLOW_DISCOURSE_ID") or os.getenv("WORKFLOW_DISCOURSE_REWRITE_ID", "")
WORKFLOW_KEYWORD_EXTRACT_ID = os.getenv("WORKFLOW_KEYWORD_EXTRACT_ID", "")
WORKFLOW_MINDMAP_ID = os.getenv("WORKFLOW_MINDMAP_ID", "")
WORKFLOW_SPEAKER_SUMMARY_ID = os.getenv("WORKFLOW_SPEAKER_SUMMARY_ID", "")
WORKFLOW_BEARER_TOKEN = os.getenv("WORKFLOW_BEARER_TOKEN", "")

# 智能体平台登录配置，兼容 `shenhe-agent - 0621` 的 AuthCredentials 方式。
LOGIN_URL = os.getenv("LOGIN_URL", "")
COMPANY_ID = os.getenv("COMPANY_ID", "")
COMPANY_INFO = os.getenv("COMPANY_INFO", "")
EMPLOYEE_INFO = os.getenv("EMPLOYEE_INFO", "")
USER_NAME = os.getenv("USER_NAME", "")
PASSWORD = os.getenv("PASSWORD", "")
RSA_PUBLIC_KEY_PATH = os.getenv("RSA_PUBLIC_KEY_PATH", str(BASE_DIR / "app" / "rsa_public.pem"))
WORKFLOW_TOKEN_CACHE_SECONDS = int(os.getenv("WORKFLOW_TOKEN_CACHE_SECONDS", "1500"))


def ensure_data_dirs() -> None:
    """确保上传和导出目录存在。"""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_CLIP_DIR.mkdir(parents=True, exist_ok=True)
