"""AI access to saved database profiles through the existing read-only job manager."""
from __future__ import annotations

import re
import time

from database_sql import DatabaseError
from terminal_tools import ToolError

CONNECTION = {'type': 'string', 'maxLength': 24, 'description': 'list_database_connections 返回的连接 ID；不接受密码或临时连接'}
DATABASE = {'type': 'string', 'maxLength': 128, 'description': '业务数据库名称；PostgreSQL 的 postgres 默认库可能没有业务表'}
SCHEMA = {'type': 'string', 'maxLength': 256}
TABLE = {'type': 'string', 'maxLength': 256}
COMMON = {'connection': CONNECTION, 'database': DATABASE}
OPERATIONS = {'get_database_catalog': 'catalog', 'get_database_structure': 'structure',
              'get_database_relationships': 'relationships', 'read_database_table': 'browse', 'query_database': 'query'}
DEFINITIONS = [
    ('list_database_connections', '列出已保存数据库连接及驱动状态，不返回密码；先确认目标连接和业务库', {}, []),
    ('get_database_catalog', '读取已保存连接可访问的数据库、Schema、表和视图；默认库为空时先在数据库列表中选择业务库',
     COMMON, ['connection']),
    ('get_database_structure', '读取指定表的字段、类型和索引，使用已保存连接并验证只读事务',
     {**COMMON, 'schema': SCHEMA, 'table': TABLE}, ['connection', 'database', 'schema', 'table']),
    ('get_database_relationships', '读取数据库实际声明的主键和外键，不根据字段名猜测关联',
     {**COMMON, 'schema': SCHEMA}, ['connection', 'database']),
    ('read_database_table', '分页读取表数据，每页最多 50 条；含敏感名称的列自动隐藏，用 has_more/page 继续',
     {**COMMON, 'schema': SCHEMA, 'table': TABLE, 'page': {'type': 'integer', 'minimum': 0, 'maximum': 1000},
      'filter_column': SCHEMA, 'filter_value': {'type': 'string', 'maxLength': 1024},
      'filter_operator': {'type': 'string', 'enum': ['contains', 'equals', 'is_null']}},
     ['connection', 'database', 'schema', 'table']),
    ('query_database', '通过只读 SQL 查询取得统计证据，仅接受单条 SELECT/只读 WITH；最多返回 200 行，不执行管理或写入语句',
     {**COMMON, 'sql': {'type': 'string', 'maxLength': 16384}}, ['connection', 'database', 'sql']),
]


def redact_result(value):
    if isinstance(value, list):
        return [redact_result(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: redact_result(item) for key, item in value.items()}
    if isinstance(result.get('columns'), list) and isinstance(result.get('rows'), list):
        hidden = [index for index, name in enumerate(result['columns']) if re.search(
            r'password|passwd|secret|token|credential|private[_-]?key|api[_-]?key|authorization|cookie', str(name), re.I)]
        if hidden:
            result['rows'] = [['[已隐藏]' if index in hidden else item for index, item in enumerate(row)] for row in result['rows']]
            result['redacted_columns'] = [result['columns'][index] for index in hidden]
    return result


class Reader:
    def __init__(self, manager, owner, *, allowed=True, scoped=False):
        self.manager, self.owner, self.allowed, self.scoped = manager, owner, allowed, scoped

    def status(self):
        return {'available': self.allowed and not self.scoped,
                'reason': '当前会话限定源码项目；切换到服务器文件范围后可查询数据库。' if self.scoped else
                          '' if self.allowed else '请先在数据库页面确认本次登录允许通过 HTTP 访问数据库，或使用 HTTPS。'}

    def execute(self, name, args, cancel):
        status = self.status()
        if not status['available']:
            raise ToolError(status['reason'])
        if cancel.is_set():
            raise ToolError('数据库证据读取已停止。')
        manager = self.manager()
        if name == 'list_database_connections':
            snapshot = manager.snapshot()
            return {'connections': [{key: item.get(key) for key in ('id', 'name', 'engine', 'host', 'port', 'user', 'database', 'tls')}
                                    for item in snapshot['connections']], 'dependencies': snapshot['dependencies'],
                    'notice': '使用已有账号在服务端建立只读事务；密码不发送给模型。连接可用不保证默认库中有业务表。'}
        if name not in OPERATIONS:
            raise ToolError('不支持的数据库只读工具。')
        key, completed = None, False
        try:
            job = manager.start(self.owner, {'id': args['connection'], 'operation': OPERATIONS[name],
                                             **{key: value for key, value in args.items() if key != 'connection'}})
            key = job['id']
            deadline = time.monotonic() + 45
            while job['state'] == 'running':
                if cancel.wait(.1):
                    raise ToolError('数据库证据读取已停止。')
                if time.monotonic() > deadline:
                    raise ToolError('数据库查询未在预期时间内完成，已请求取消，请缩小查询范围。')
                job = manager.job(self.owner, key)
            if job['state'] != 'succeeded':
                raise ToolError(job.get('error') or '数据库只读查询未完成。')
            completed = True
            return {**redact_result(job['result']), 'connection': args['connection'],
                    'database': args.get('database', job['result'].get('database', '')),
                    'elapsed_ms': job.get('elapsed_ms'), 'read_only': True,
                    'notice': job['result'].get('notice', '') + ' 当前数据库证据；表数据并发变化、结果分页或截断时不能当作完整快照。'}
        except DatabaseError as exc:
            raise ToolError(str(exc)) from exc
        finally:
            if key and not completed:
                try:
                    manager.cancel(self.owner, key)
                except DatabaseError:
                    pass
