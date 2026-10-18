"""未提交PDF片的API授权边界；纯Stub，不启动服务、不连接数据库或请求MinerU。"""

from uuid import uuid4

import pytest


@pytest.mark.parametrize("allow", [False, True])
def test_retry_forwards_explicit_new_split_permission_only(monkeypatch, allow):
    """默认请求不携带新收费许可，显式true才传给既有管理编排。"""
    from app.api import admin_documents as api
    calls = []

    def submit(session, doc_id, operation, parse_mode, **options):
        """记录薄API转发，不执行管理任务主体。"""
        calls.append((operation, options))
        return "accepted-stub"

    monkeypatch.setattr(api, "submit_management", submit)
    assert api.retry_document(uuid4(), object(), allow_new_split_parts=allow) == "accepted-stub"
    assert calls == [("retry", {"allow_new_split_parts": True} if allow else {})]
