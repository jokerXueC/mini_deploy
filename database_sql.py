"""A deliberately small SELECT dialect, backed by database read-only transactions."""
from __future__ import annotations

try:
    import sqlglot
    from sqlglot import exp
except ImportError:
    sqlglot = exp = None


class DatabaseError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


STRUCTURE = frozenset('''Select Union Intersect Except Column Table Identifier Literal Star From Join
Where Group Having Order Ordered Limit Offset With CTE Subquery TableAlias Alias Paren Distinct
And Or Not EQ NEQ GT GTE LT LTE Is In Between Like ILike Escape Add Sub Mul Div Mod Neg Null Boolean
Case If DataType DataTypeParam Tuple Window WindowSpec Filter WithinGroup'''.split())
FUNCTIONS = frozenset('''COUNT SUM AVG MIN MAX COALESCE NULLIF CAST ROUND ABS CEIL FLOOR LOWER UPPER IF CASE
LENGTH CHAR_LENGTH SUBSTRING TRIM LTRIM RTRIM CONCAT CONCAT_WS DATE_TRUNC EXTRACT
CURRENT_DATE CURRENT_TIMESTAMP ROW_NUMBER RANK DENSE_RANK'''.split())
TYPES = frozenset('''TEXT VARCHAR CHAR NCHAR NVARCHAR INT SMALLINT BIGINT TINYINT FLOAT DOUBLE DECIMAL
BOOLEAN DATE TIMESTAMP TIMESTAMPTZ DATETIME JSON JSONB BINARY VARBINARY'''.split())


def select_sql(text, engine):
    if sqlglot is None:
        raise DatabaseError('只读 SQL 解析组件未安装，请按数据库指南安装 ui/requirements-database.txt。', 503)
    if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 16384 or '\x00' in text:
        raise DatabaseError('请输入一条 SELECT 查询，SQL 最多 16 KiB。')
    dialect = 'postgres' if engine == 'postgres' else 'mysql'
    try:
        # Unsupported-command warnings must not log SQL literals or credentials.
        statements = sqlglot.parse(text, read=dialect, error_level=sqlglot.ErrorLevel.RAISE, error_message_context=0)
    except (sqlglot.errors.SqlglotError, RecursionError, ValueError):
        raise DatabaseError('SQL 无法安全解析；请检查语法，只执行单条 SELECT。') from None
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise DatabaseError('只读模式仅接受单条 SELECT，可使用只读 WITH；不支持写入、管理语句或多条语句。')
    tree = statements[0]
    nodes = list(tree.walk())
    if len(nodes) > 2500:
        raise DatabaseError('查询结构过于复杂，请缩小查询范围。')
    for node in nodes:
        if node.comments:
            raise DatabaseError('只读查询不接受注释、优化器提示或版本条件语句。')
        if node.depth > 60:
            raise DatabaseError('查询嵌套过深，请缩小查询范围。')
        if isinstance(node, exp.Func):
            if isinstance(node, exp.Anonymous):
                raise DatabaseError('只读初版不执行自定义函数。')
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
            if name not in FUNCTIONS:
                raise DatabaseError(f'只读初版暂不支持函数 {name[:60]}；不执行存储函数、文件或网络调用。')
        elif type(node).__name__ not in STRUCTURE:
            raise DatabaseError(f'只读初版不支持此 SQL 结构（{type(node).__name__}）。')
        if isinstance(node, exp.DataType) and node.this.value not in TYPES:
            raise DatabaseError('只读初版不支持此类型转换。')
        if isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
            raise DatabaseError('只能查询普通表、视图或只读 WITH，不能调用表函数。')
    try:
        return tree.sql(dialect=dialect, unsupported_level=sqlglot.ErrorLevel.RAISE)
    except (sqlglot.errors.SqlglotError, ValueError, AttributeError):
        raise DatabaseError('该查询无法安全转换为目标数据库语法。') from None
