"""检索数据库安全分类的内存回归，由用户运行，不连接数据库或读取原件。"""

import json

import pytest
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.services.rag.bm25 import BM25RetrievalError
from app.services.rag.database import database_diagnostic
from app.services.rag.dense import DenseRetrievalError, select_dense_hits
from bm25_stubs import StreamingResultStub, StreamingSessionStub
from test_dense_sql import SqlSessionStub, model_identity, stored_pair


def driver_error(state, field="sqlstate"):
    """模拟psycopg3/2携带SQLSTATE的驱动错误，私密消息仅用于验证脱敏。"""
    original = RuntimeError("PRIVATE_PASSWORD_QUERY_CONNECTION")
    setattr(original, field, state)
    return DBAPIError("PRIVATE_SQL", {"password": "PRIVATE_PASSWORD"}, original)


@pytest.mark.parametrize("state,reason", [("57014", "QUERY_CANCELED"), ("55P03", "LOCK_UNAVAILABLE"),
    ("08006", "CONNECTION"), ("57P01", "CONNECTION"), ("53200", "RESOURCE"), ("22P02", "OTHER"),
    (None, "OTHER"), ("PRIVATE_PASSWORD", "OTHER")])
@pytest.mark.parametrize("field", ["sqlstate", "pgcode"])
@pytest.mark.parametrize("error_type,prefix", [(DenseRetrievalError, "DENSE"), (BM25RetrievalError, "BM25")])
def test_database_classification_preserves_only_safe_state(state, reason, field, error_type, prefix):
    """分类兼容两种驱动，57014不等于已证实超时，输出不包含SQL或原始异常。"""
    error = error_type(prefix + "_DATABASE", database_error=driver_error(state, field))
    expected_state = state if state and len(state) == 5 else None
    assert error.database_diagnostic == {"reason": reason, "sqlstate": expected_state}
    assert error.code == prefix + "_DATABASE" + ("_" + reason if reason != "OTHER" else "")
    assert "PRIVATE" not in str(error) + json.dumps(error.database_diagnostic)
    assert database_diagnostic(SQLAlchemyError("PRIVATE_CONNECTION")) == {"reason": "OTHER", "sqlstate": None}


def test_dense_keeps_original_state_at_first_sql_boundary():
    """Dense首次捕获驱动异常即保留SQLSTATE，外层不用猜测十秒耗时。"""
    class FailedSession(SqlSessionStub):
        """只注入驱动故障，不创建真实数据库会话。"""

        def execute(self, statement):
            """模拟数据库取消SELECT，禁止返回半份候选。"""
            raise driver_error("57014")

    pair = stored_pair()
    with pytest.raises(DenseRetrievalError, match="DENSE_DATABASE_QUERY_CANCELED") as caught:
        select_dense_hits(FailedSession([]), pair[0].kb_id, [1.0] + [0.0] * 1023, model_identity())
    assert caught.value.database_diagnostic["sqlstate"] == "57014" and caught.value.__suppress_context__


def test_fetch_failure_is_not_replaced_by_cursor_close_failure():
    """拉取取消后关闭连接也失败时，仍保留第一次SQLSTATE与固定错误码。"""
    from app.services.rag.bm25 import select_sources

    class FailedResult(StreamingResultStub):
        """先返回一条来源，再模拟取批与清理连续失败。"""

        def __iter__(self):
            """将真实根因设置为查询取消。"""
            yield self.rows[0]
            raise driver_error("57014")

        def close(self):
            """标记清理已尝试，次生连接错误不得覆盖取消原因。"""
            super().close()
            raise driver_error("08006")

    pair = stored_pair()
    session = StreamingSessionStub([pair])
    session.result_type = FailedResult
    with pytest.raises(BM25RetrievalError, match="BM25_DATABASE_QUERY_CANCELED") as caught:
        select_sources(session, pair[0].kb_id)
    assert caught.value.database_diagnostic == {"reason": "QUERY_CANCELED", "sqlstate": "57014"}
    assert session.results[0].closed and "PRIVATE" not in str(caught.value)
