"""仅在文档提交后投递任务，投递失败不删除已成功上传的原件。"""

from uuid import UUID

from kombu.exceptions import OperationalError
from redis.exceptions import RedisError
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import Document
from app.tasks.documents import process_document

PUBLISH_ERROR = "异步任务投递失败，请检查 Redis 与 CPU Worker 配置后重新投递"


def publish_document(session: Session, doc_id: UUID, task_id: UUID) -> bool:
    """投递携带当前任务标识的消息；投递失败仅标记尚未开始的当前任务为 FAILED。"""

    try:
        process_document.apply_async(
            args=[str(doc_id)], task_id=str(task_id), queue="default_queue"
        )
        return True
    except (OperationalError, RedisError, OSError):
        # 若消息已被 Worker 接收并推进状态，不能因发送端确认丢失而覆盖 Worker 的结果。
        session.execute(
            update(Document)
            .where(
                Document.doc_id == doc_id,
                Document.processing_task_id == task_id,
                Document.status == "UPLOADED",
            )
            .values(status="FAILED", error_message=PUBLISH_ERROR)
        )
        session.commit()
        return False
