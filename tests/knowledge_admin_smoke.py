"""显式执行的管理员接口验收，复用隔离迁移 fixture，不向 public 写入测试数据。"""

import asyncio
from datetime import datetime
from operator import itemgetter
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.api import admin_knowledge_bases as admin_api
from app.main import app
from app.models import Document

# 该模块不使用 test_ 前缀，数据库验收需显式指定文件；共享 fixture 仍按当前模块隔离。
pytest_plugins = ("knowledge_db_smoke",)
PREFIX = "/api/admin/knowledge-bases"


def api_request(method, path, **kwargs):
    """通过 ASGI 调用真实应用路由，完成一次请求后关闭 HTTP 客户端。"""

    async def request():
        """使用项目已声明的 httpx 在本进程请求应用，不启动额外 HTTP 服务。"""

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(request())


@pytest.fixture
def api(migrated_connection, monkeypatch):
    """每个用例使用外层保存点，各请求独立提交会话保存点，结束后回滚整个用例。"""

    connection, _, _ = migrated_connection
    transaction = connection.begin_nested()

    def isolated_session():
        """给接口提供绑定测试连接的独立会话，使 commit 不提交外层隔离事务。"""

        with Session(connection, join_transaction_mode="create_savepoint") as session:
            yield session

    # 替换数据库会话来源而不替换错误处理依赖，保证 503 路径也经过真实实现。
    monkeypatch.setattr(admin_api, "get_session", isolated_session)
    try:
        yield api_request
    finally:
        transaction.rollback()


def create_kb(api, name, description=None):
    """创建接口测试所需知识库并返回 JSON，创建失败时立即中断后续断言。"""

    response = api("POST", PREFIX, json={"name": name, "description": description})
    assert response.status_code == 201, response.text
    return response.json()


def test_create_detail_and_empty_documents(api):
    """验证创建字段规范、跨请求持久化读取及存在但尚无文档时的空分页响应。"""

    kb = create_kb(api, "  研究资料  ", "参考说明")
    UUID(kb["kb_id"])
    assert kb["name"] == "研究资料"
    assert kb["description"] == "参考说明"
    assert kb["status"] == "ACTIVE"
    assert datetime.fromisoformat(kb["created_at"]).tzinfo is not None
    assert datetime.fromisoformat(kb["updated_at"]).tzinfo is not None
    response = api("GET", f"{PREFIX}/{kb['kb_id']}")
    assert response.status_code == 200
    assert response.json() == kb
    documents = api("GET", f"{PREFIX}/{kb['kb_id']}/documents")
    assert documents.status_code == 200
    assert documents.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_optional_description_and_empty_kb_list(api):
    """验证空知识库列表及创建请求省略可选描述时的默认值。"""

    response = api("GET", PREFIX)
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
    response = api("POST", PREFIX, json={"name": "无描述"})
    assert response.status_code == 201
    assert response.json()["description"] is None


def test_duplicate_name_is_409_and_next_request_can_write(api):
    """验证规范化后的名称重复返回 409，失败回滚后仍可创建其他知识库。"""

    first = create_kb(api, "重复资料")
    response = api("POST", PREFIX, json={"name": "  重复资料  "})
    assert response.status_code == 409
    assert response.json() == {"detail": "知识库名称已存在"}
    second = create_kb(api, "另一份资料")
    assert first["kb_id"] != second["kb_id"]
    assert api("GET", PREFIX).json()["total"] == 2


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": ""},
        {"name": " \t\n "},
        {"name": "\u3000"},
        {"name": "a" * 256},
        {"name": None},
        {"name": 123},
        {"name": True},
        {"name": "资料", "description": 123},
        {"name": "资料", "status": "DISABLED"},
        {"name": "资料", "kb_id": str(uuid4())},
    ],
)
def test_invalid_create_does_not_write(api, payload):
    """验证非法名称、字段类型或额外字段均返回 422，且不生成任何知识库。"""

    assert api("POST", PREFIX, json=payload).status_code == 422
    assert api("GET", PREFIX).json()["total"] == 0


@pytest.mark.parametrize("suffix", ["", "/documents"])
def test_unknown_knowledge_base_is_404(api, suffix):
    """验证合法但不存在的知识库标识返回 404，不误当作空文档列表。"""

    response = api("GET", f"{PREFIX}/{uuid4()}{suffix}")
    assert response.status_code == 404
    assert response.json() == {"detail": "知识库不存在"}


@pytest.mark.parametrize("suffix", ["", "/documents"])
def test_invalid_knowledge_base_id_is_422(api, suffix):
    """验证详情和文档列表均拒绝非法 UUID 路径参数。"""

    assert api("GET", f"{PREFIX}/not-a-uuid{suffix}").status_code == 422


def test_knowledge_base_pagination_has_stable_order(api):
    """验证总数、分页窗口及创建时间相同情况下的 UUID 排序，越界窗口返回空列表。"""

    created = [create_kb(api, f"资料-{index}") for index in range(3)]
    expected = sorted(created, key=itemgetter("kb_id"), reverse=True)
    response = api("GET", PREFIX, params={"limit": 2, "offset": 1})
    assert response.status_code == 200
    assert response.json() == {
        "items": expected[1:],
        "total": 3,
        "limit": 2,
        "offset": 1,
    }
    assert api("GET", PREFIX, params={"offset": 100}).json()["items"] == []


@pytest.mark.parametrize("documents", [False, True])
@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 101}, {"limit": "bad"}, {"offset": -1}]
)
def test_invalid_pagination_is_422(api, documents, params):
    """验证两个列表接口的条数边界和偏移量校验一致。"""

    kb = create_kb(api, "分页资料")
    path = f"{PREFIX}/{kb['kb_id']}/documents" if documents else PREFIX
    assert api("GET", path, params=params).status_code == 422


def test_document_listing_filters_kb_and_preserves_metadata(api, migrated_connection):
    """验证文档列表不会跨库，正确分页并保留状态、来源与失败信息，同时隐藏文件路径。"""

    first = create_kb(api, "文档所属库")
    second = create_kb(api, "其他库")
    connection, _, _ = migrated_connection
    doc_ids = [uuid4(), uuid4()]
    with Session(connection, join_transaction_mode="create_savepoint") as session:
        for index, doc_id in enumerate(doc_ids):
            session.add(
                Document(
                    doc_id=doc_id,
                    kb_id=UUID(first["kb_id"]),
                    file_name=f"参考-{index}.pdf",
                    file_type="pdf",
                    file_hash=str(index) * 64,
                    file_path="private/reference.pdf",
                    source_type="preset" if index == 0 else "uploaded",
                    status="READY" if index == 0 else "FAILED",
                    error_message=None if index == 0 else "文档无法解析",
                )
            )
        session.add(
            Document(
                kb_id=UUID(second["kb_id"]),
                file_name="其他库.txt",
                file_type="txt",
                file_hash="2" * 64,
                file_path="private/other.txt",
                source_type="preset",
            )
        )
        session.commit()
    path = f"{PREFIX}/{first['kb_id']}/documents"
    response = api("GET", path)
    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 2
    assert [item["doc_id"] for item in page["items"]] == sorted(
        map(str, doc_ids), reverse=True
    )
    assert {item["source_type"] for item in page["items"]} == {"preset", "uploaded"}
    assert {item["status"] for item in page["items"]} == {"READY", "FAILED"}
    assert {item["error_message"] for item in page["items"]} == {None, "文档无法解析"}
    assert all(item["kb_id"] == first["kb_id"] for item in page["items"])
    assert all(
        "file_path" not in item and "embedding" not in item for item in page["items"]
    )
    window = api("GET", path, params={"limit": 1, "offset": 1}).json()
    assert window == {"items": page["items"][1:], "total": 2, "limit": 1, "offset": 1}


@pytest.mark.parametrize(
    "method, suffix",
    [("POST", ""), ("GET", ""), ("GET", "/id"), ("GET", "/id/documents")],
)
def test_database_query_failure_is_sanitized(api, monkeypatch, method, suffix):
    """验证会话已建立后各接口的数据库操作失败返回 503，且不泄漏凭据或 SQL。"""

    def unavailable(*args, **kwargs):
        """模拟数据库查询或提交失败，底层异常刻意携带敏感连接信息。"""

        raise OperationalError(
            "SELECT private_data", None, Exception("postgresql://secret:password@host")
        )

    monkeypatch.setattr(Session, "get", unavailable)
    monkeypatch.setattr(Session, "scalar", unavailable)
    monkeypatch.setattr(Session, "commit", unavailable)
    path = PREFIX + suffix.replace("/id", f"/{uuid4()}")
    kwargs = {"json": {"name": "失败请求"}} if method == "POST" else {}
    response = api(method, path, **kwargs)
    assert response.status_code == 503
    assert response.json()["detail"] == "知识库数据库不可用，请检查数据库连接与迁移状态"
    assert "password" not in response.text and "private_data" not in response.text


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, OperationalError])
def test_database_initialization_failure_is_503(api, monkeypatch, error_type):
    """验证配置或会话初始化失败也返回统一 503，避免依赖初始化绕过异常处理。"""

    def unavailable_session():
        """在提供会话之前模拟失败，异常消息不得原样传入 HTTP 响应。"""

        if error_type is OperationalError:
            raise OperationalError("private_sql", None, Exception("secret_password"))
        raise error_type("secret_password")
        yield  # 保持与数据库会话依赖相同的生成器协议。

    monkeypatch.setattr(admin_api, "get_session", unavailable_session)
    response = api("GET", PREFIX)
    assert response.status_code == 503
    assert "secret_password" not in response.text


def test_other_integrity_error_is_not_name_conflict(api, monkeypatch):
    """验证非名称唯一约束错误不会被误报为 409，失败详情仍由数据库异常边界隐藏。"""

    def invalid_commit(session):
        """模拟其他完整性错误，确保只有指定名称约束被归类为重复创建。"""

        raise IntegrityError("private_sql", None, Exception("secret_password"))

    monkeypatch.setattr(Session, "commit", invalid_commit)
    response = api("POST", PREFIX, json={"name": "其他错误"})
    assert response.status_code == 503
    assert "secret_password" not in response.text


def test_openapi_contains_all_admin_routes(api):
    """验证四个接口已注册到应用及 Swagger，并声明正确的成功响应。"""

    response = api("GET", "/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "201" in paths[PREFIX]["post"]["responses"]
    assert "200" in paths[PREFIX]["get"]["responses"]
    assert "200" in paths[f"{PREFIX}/{{kb_id}}"]["get"]["responses"]
    assert "200" in paths[f"{PREFIX}/{{kb_id}}/documents"]["get"]["responses"]
    assert "409" in paths[PREFIX]["post"]["responses"]
    assert "404" in paths[f"{PREFIX}/{{kb_id}}/documents"]["get"]["responses"]
    assert "503" in paths[PREFIX]["get"]["responses"]
