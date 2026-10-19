"""BM25分批只读结果的内存Stub；不连接数据库、不加载模型或生成文档。"""

from test_dense_sql import SqlSessionStub


class StreamingResultStub:
    """模拟可迭代、可显式关闭的SQL结果，禁止退回全量all()读取。"""

    def __init__(self, rows):
        """保留预设行及关闭标记，不复制完整语料。"""
        self.rows, self.closed = rows, False

    def __iter__(self):
        """按预设顺序逐行返回，保留坏行和重复行供生产校验。"""
        return iter(self.rows)

    def all(self):
        """全量物化结果应立即失败，确保受测路径使用分批迭代。"""
        raise AssertionError('BM25来源不得调用all()物化整库')

    def close(self):
        """记录游标释放，便于检查成功及异常路径。"""
        self.closed = True


class StreamingSessionStub(SqlSessionStub):
    """记录只读快照设置及命中ID过滤，不执行SQL或修改预设来源。"""

    result_type = StreamingResultStub

    def __init__(self, rows, *, failure=None):
        """初始化查询、游标及故障观测字段。"""
        super().__init__(rows)
        self.settings, self.results = [], []
        self.closed, self.failure = False, failure

    def __enter__(self):
        """进入独立内存会话，不启动服务或真实事务。"""
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """标记会话结束，不提交或打印底层异常。"""
        self.closed = True

    def in_transaction(self):
        """模拟尚未开启事务的独立会话。"""
        return False

    def execute(self, statement):
        """记录设置或返回逐行结果；仅模拟生产SQL中的明确命中ID条件。"""
        if not statement.is_select:
            self.settings.append(str(statement))
            return self
        if self.failure is not None:
            raise self.failure
        super().execute(statement)
        params = statement.compile().params
        chunk_ids = next((value for key, value in params.items() if key.startswith('chunk_id_')), None)
        rows = self.rows if chunk_ids is None else [row for row in self.rows if row[0].chunk_id in chunk_ids]
        result = self.result_type(rows)
        self.results.append(result)
        return result


def factory_for(rows, *, failure=None):
    """为每次查询创建独立可观察会话，所有内容仅来自调用方内存字段。"""
    sessions = []

    def factory():
        """构造本次查询的内存会话，不连接业务数据库。"""
        session = StreamingSessionStub(rows, failure=failure)
        sessions.append(session)
        return session

    return factory, sessions
