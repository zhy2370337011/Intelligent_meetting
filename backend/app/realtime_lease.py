"""进程内实时会议单活租约。

浏览器重连、双击开始或旧页面未关闭时，同一 meeting 可能同时存在多个 WebSocket。只有
最后一次声明配置的连接可以落库，才能避免重复片段和交叉说话人状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class RealtimeLease:
    """一条不可变的 meeting 实时写入所有权。"""

    owner_id: str
    session_token: str


class RealtimeLeaseRegistry:
    """维护当前 Python 进程内每个 meeting 的唯一实时写入者。

    该注册器只解决单 Uvicorn 进程内的并发连接。若部署多个 worker 或多台业务服务器，
    必须把 claim/is_owner/compare-and-release 迁移到 Redis 或数据库条件更新；不能误以为
    进程内字典能跨进程提供全局互斥。
    """

    def __init__(self) -> None:
        self._leases: dict[str, RealtimeLease] = {}
        # 当前调用主要来自同一 asyncio 事件循环，但同步 helper 和测试也可能跨线程进入。
        # 普通 Lock 足以保护几个无 await 的字典操作，不会把网络等待放在临界区内。
        self._lock = Lock()

    def claim(self, meeting_id: str, *, owner_id: str, session_token: str) -> RealtimeLease | None:
        """原子替换 meeting 所有者，并返回被替换的旧租约。"""

        lease = RealtimeLease(owner_id=str(owner_id), session_token=str(session_token))
        with self._lock:
            previous = self._leases.get(str(meeting_id))
            self._leases[str(meeting_id)] = lease
            return previous

    def is_owner(self, meeting_id: str, *, owner_id: str, session_token: str) -> bool:
        """同时比较连接身份和 token，防止不同连接复用 token 冒充所有者。"""

        expected = RealtimeLease(owner_id=str(owner_id), session_token=str(session_token))
        with self._lock:
            return self._leases.get(str(meeting_id)) == expected

    def release(self, meeting_id: str, *, owner_id: str, session_token: str) -> bool:
        """仅当当前值仍等于调用方租约时删除，避免旧 finally 清掉新连接。"""

        expected = RealtimeLease(owner_id=str(owner_id), session_token=str(session_token))
        with self._lock:
            if self._leases.get(str(meeting_id)) != expected:
                return False
            del self._leases[str(meeting_id)]
            return True
