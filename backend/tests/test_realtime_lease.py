"""同一会议实时连接单活所有权的纯单元测试。"""

from __future__ import annotations

import unittest

from app.realtime_lease import RealtimeLeaseRegistry


class RealtimeLeaseRegistryTest(unittest.TestCase):
    """验证后来连接接管、旧连接失权和 compare-and-release。"""

    def test_later_connection_invalidates_old_owner_and_old_release_cannot_delete_new_lease(self) -> None:
        """旧连接 finally 迟到时，只能释放自己的租约，不能清掉新连接。"""

        registry = RealtimeLeaseRegistry()
        registry.claim("meeting-1", owner_id="socket-old", session_token="token-old")
        self.assertTrue(registry.is_owner("meeting-1", owner_id="socket-old", session_token="token-old"))

        registry.claim("meeting-1", owner_id="socket-new", session_token="token-new")

        self.assertFalse(registry.is_owner("meeting-1", owner_id="socket-old", session_token="token-old"))
        self.assertTrue(registry.is_owner("meeting-1", owner_id="socket-new", session_token="token-new"))
        self.assertFalse(registry.release("meeting-1", owner_id="socket-old", session_token="token-old"))
        self.assertTrue(registry.is_owner("meeting-1", owner_id="socket-new", session_token="token-new"))
        self.assertTrue(registry.release("meeting-1", owner_id="socket-new", session_token="token-new"))
        self.assertFalse(registry.is_owner("meeting-1", owner_id="socket-new", session_token="token-new"))


if __name__ == "__main__":
    unittest.main()
