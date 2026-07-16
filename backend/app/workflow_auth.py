"""智能体平台认证。

本模块兼容 `E:\\work\\my-todo\\shenhe-agent - 0621` 的登录方式：
使用 RSA 公钥加密密码，调用第三方登录接口获取 access_token。为了本地测试方便，
也支持直接配置 `WORKFLOW_BEARER_TOKEN`，此时不会发起登录请求。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from app.config import (
    COMPANY_ID,
    COMPANY_INFO,
    EMPLOYEE_INFO,
    LOGIN_URL,
    PASSWORD,
    RSA_PUBLIC_KEY_PATH,
    USER_NAME,
    WORKFLOW_BEARER_TOKEN,
    WORKFLOW_TOKEN_CACHE_SECONDS,
)


Urlopen = Callable[..., Any]


class WorkflowAuthError(RuntimeError):
    """智能体平台认证失败。"""


class WorkflowAuthProvider:
    """智能体平台 token 提供器。

    生产环境推荐通过后端环境变量或密钥管理系统注入账号信息。本类只负责把配置
    转成 token，不在代码中保存任何明文密钥。
    """

    def __init__(self, urlopen: Urlopen = urllib.request.urlopen):
        self.urlopen = urlopen
        self._cached_token = ""
        self._cached_at = 0.0

    def get_token(self) -> str:
        """获取 Bearer token。

        优先返回 `WORKFLOW_BEARER_TOKEN`；否则使用登录配置换取 token，并在内存中
        缓存一段时间，避免每次调用工作流都登录。
        """

        if WORKFLOW_BEARER_TOKEN:
            return WORKFLOW_BEARER_TOKEN
        if self._cached_token and time.time() - self._cached_at < WORKFLOW_TOKEN_CACHE_SECONDS:
            return self._cached_token
        self._cached_token = self._login_for_token()
        self._cached_at = time.time()
        return self._cached_token

    def _login_for_token(self) -> str:
        """调用智能体平台登录接口换取 token。"""

        missing = [
            name
            for name, value in {
                "LOGIN_URL": LOGIN_URL,
                "COMPANY_ID": COMPANY_ID,
                "COMPANY_INFO": COMPANY_INFO,
                "EMPLOYEE_INFO": EMPLOYEE_INFO,
                "USER_NAME": USER_NAME,
                "PASSWORD": PASSWORD,
                "RSA_PUBLIC_KEY_PATH": RSA_PUBLIC_KEY_PATH,
            }.items()
            if not value
        ]
        if missing:
            raise WorkflowAuthError(f"智能体平台登录配置不完整：{', '.join(missing)}")

        payload = {
            "company_id": COMPANY_ID,
            "company_info": COMPANY_INFO,
            "employee_info": EMPLOYEE_INFO,
            "user_name": USER_NAME,
            "encrypted_password": self._encrypt_password(PASSWORD),
        }
        request = urllib.request.Request(
            LOGIN_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8") or "{}")
        try:
            return result["data"]["access_token"]
        except KeyError as exc:
            raise WorkflowAuthError(f"智能体平台登录响应缺少 access_token：{result}") from exc

    def _encrypt_password(self, password: str) -> str:
        """按平台要求 RSA 加密密码。

        这里优先使用 `rsa` 包；如果服务器没有安装，会给出明确错误。由于登录换 token
        只在未提供 `WORKFLOW_BEARER_TOKEN` 时发生，本地也可以先直接配置 token 跳过。
        """

        try:
            import rsa  # type: ignore
        except ImportError as exc:
            raise WorkflowAuthError("未安装 rsa 依赖，无法使用账号密码登录智能体平台") from exc

        public_key_path = Path(RSA_PUBLIC_KEY_PATH)
        if not public_key_path.is_absolute():
            public_key_path = Path(__file__).resolve().parent.parent / public_key_path
        if not public_key_path.exists():
            raise WorkflowAuthError(f"RSA 公钥文件不存在：{public_key_path}")

        public_key = rsa.PublicKey.load_pkcs1(public_key_path.read_bytes())
        raw = json.dumps({"data": password}, ensure_ascii=False).encode("utf-8")
        return base64.b64encode(rsa.encrypt(raw, public_key)).decode("utf-8")


def bearer_token_or_empty() -> str:
    """给工作流客户端使用的安全 token 获取函数。"""

    try:
        return WorkflowAuthProvider().get_token()
    except WorkflowAuthError:
        return ""
