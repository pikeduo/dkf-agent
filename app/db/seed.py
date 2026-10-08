"""部署时显式创建默认知识库，重复执行不会产生重复记录。"""

from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.session import get_engine
from app.models import KnowledgeBase

DEFAULT_KB_ID = UUID("7f2044ca-2041-42ce-977d-40bf5c79ed40")


def ensure_default_knowledge_base(session: Session) -> KnowledgeBase:
    """在传入会话中幂等创建并返回默认知识库，不覆盖已有记录，也不自行提交事务。

    固定主键对应记录在插入后仍无法读取时抛出异常，由调用方处理或回滚。
    """

    # 固定主键与 ON CONFLICT 保证重复部署或并发初始化不会创建两个默认库。
    session.execute(
        insert(KnowledgeBase)
        .values(
            kb_id=DEFAULT_KB_ID,
            name="默认知识库",
            description="预置知识库文档的默认归属",
            status="ACTIVE",
        )
        .on_conflict_do_nothing(index_elements=["kb_id"])
    )
    kb = session.get(KnowledgeBase, DEFAULT_KB_ID)
    if kb is None:
        raise RuntimeError("默认知识库初始化后无法读取，请检查数据库事务")
    return kb


def main() -> None:
    """在独立事务中初始化默认知识库并输出标识，成功时提交、异常时回滚。"""

    with Session(get_engine()) as session, session.begin():
        kb = ensure_default_knowledge_base(session)
        print(f"默认知识库已就绪：kb_id={kb.kb_id}")


if __name__ == "__main__":
    main()
