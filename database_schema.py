"""Read table metadata and declared foreign keys without sampling business rows."""
from collections import OrderedDict

from database_sql import DatabaseError

TABLES, COLUMNS, KEYS = 500, 10000, 5000


def relationships(session, schema=''):
    pg = session.spec['engine'] == 'postgres'
    database = session.spec['database'] or ('postgres' if pg else '')
    if not database:
        raise DatabaseError('请先选择数据库。')
    if not pg and schema and schema != database:
        raise DatabaseError('关系图仅查看当前选中的数据库。')
    params = []
    where = "table_schema NOT IN ('pg_catalog','information_schema')" if pg else 'table_schema=%s'
    if not pg:
        params.append(database)
    elif schema:
        where += ' AND table_schema=%s'
        params.append(schema)
    catalog = session.query('SELECT table_schema,table_name,table_type FROM information_schema.tables '
                            f'WHERE {where} ORDER BY table_schema,table_name LIMIT {TABLES + 1}', tuple(params), maximum=TABLES)
    nodes = OrderedDict()
    for row in catalog['rows']:
        key = tuple(row[:2])
        nodes[key] = dict(id=f't{len(nodes)}', schema=key[0], name=key[1], type=row[2], columns=[])
    if not nodes:
        return dict(database=database, tables=[], relationships=[], truncated=catalog['truncated'],
                    omitted_references=0, notice='当前账号在此范围内没有可查看的表。', read_only=True)

    objects = ','.join(['(%s,%s)'] * len(nodes))
    params = tuple(value for key in nodes for value in key)
    columns = session.query('SELECT table_schema,table_name,column_name,data_type,is_nullable '
        f'FROM information_schema.columns WHERE (table_schema,table_name) IN ({objects}) '
        f'ORDER BY table_schema,table_name,ordinal_position LIMIT {COLUMNS + 1}', params, maximum=COLUMNS)
    fields = {}
    for schema_name, table, name, data_type, nullable in columns['rows']:
        node = nodes.get((schema_name, table))
        if node is not None:
            field = dict(name=name, type=data_type, nullable=nullable == 'YES', primary_key=False, foreign_key=False)
            node['columns'].append(field)
            fields[(schema_name, table, name)] = field

    if pg:
        sql = '''SELECT CASE c.contype WHEN 'p' THEN 'PRIMARY KEY' ELSE 'FOREIGN KEY' END,
            c.conname,n.nspname,t.relname,a.attname,k.ord,rn.nspname,r.relname,ra.attname,
            c.confupdtype,c.confdeltype
            FROM pg_catalog.pg_constraint c
            JOIN pg_catalog.pg_class t ON t.oid=c.conrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
            JOIN LATERAL unnest(c.conkey) WITH ORDINALITY k(attnum,ord) ON TRUE
            JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum
            LEFT JOIN pg_catalog.pg_class r ON r.oid=c.confrelid
            LEFT JOIN pg_catalog.pg_namespace rn ON rn.oid=r.relnamespace
            LEFT JOIN pg_catalog.pg_attribute ra ON ra.attrelid=r.oid AND ra.attnum=c.confkey[k.ord::int]
            WHERE c.contype IN ('p','f') AND (n.nspname,t.relname) IN (''' + objects + ''')
            ORDER BY n.nspname,t.relname,c.conname,k.ord'''
    else:
        sql = '''SELECT tc.constraint_type,k.constraint_name,k.table_schema,k.table_name,k.column_name,
            k.ordinal_position,k.referenced_table_schema,k.referenced_table_name,k.referenced_column_name,
            rc.update_rule,rc.delete_rule
            FROM information_schema.key_column_usage k
            JOIN information_schema.table_constraints tc ON tc.constraint_schema=k.constraint_schema
                AND tc.table_schema=k.table_schema AND tc.table_name=k.table_name AND tc.constraint_name=k.constraint_name
            LEFT JOIN information_schema.referential_constraints rc ON rc.constraint_schema=k.constraint_schema
                AND rc.table_name=k.table_name AND rc.constraint_name=k.constraint_name
            WHERE tc.constraint_type IN ('PRIMARY KEY','FOREIGN KEY') AND (k.table_schema,k.table_name) IN (''' + objects + ''')
            ORDER BY k.table_schema,k.table_name,k.constraint_name,k.ordinal_position'''
    keys = session.query(sql + f' LIMIT {KEYS + 1}', params, maximum=KEYS)
    groups = OrderedDict()
    for kind, name, source_schema, source_table, source_col, ordinal, target_schema, target_table, target_col, update, delete in keys['rows']:
        field = fields.get((source_schema, source_table, source_col))
        if field is not None:
            field['primary_key' if kind == 'PRIMARY KEY' else 'foreign_key'] = True
        if kind != 'FOREIGN KEY':
            continue
        key = (source_schema, source_table, name)
        if key not in groups:
            groups[key] = dict(name=name, source=(source_schema, source_table), target=(target_schema, target_table),
                               columns=[], target_columns=[], update_rule=update, delete_rule=delete)
        groups[key]['columns'].append(source_col)
        groups[key]['target_columns'].append(target_col)
    # A row/byte boundary can cut a composite FK in half; never render that as a complete key.
    if keys['truncated'] and keys['rows']:
        last = keys['rows'][-1]
        groups.pop((last[2], last[3], last[1]), None)
    edges, omitted = [], 0
    rules = {'a': 'NO ACTION', 'r': 'RESTRICT', 'c': 'CASCADE', 'n': 'SET NULL', 'd': 'SET DEFAULT'}
    for group in groups.values():
        source, target = nodes.get(group['source']), nodes.get(group['target'])
        if source is None or target is None:
            omitted += 1
            continue
        edges.append({**group, 'id': f'f{len(edges)}', 'source': source['id'], 'target': target['id'],
                      'update_rule': rules.get(group['update_rule'], group['update_rule'] or '未知'),
                      'delete_rule': rules.get(group['delete_rule'], group['delete_rule'] or '未知')})
    truncated = any(result['truncated'] for result in (catalog, columns, keys))
    notices = []
    if truncated:
        notices.append(f'元数据未全部载入（最多 {TABLES} 张表、{COLUMNS} 个字段、{KEYS} 条键列记录），可按 Schema 缩小范围。')
    if omitted:
        notices.append(f'{omitted} 条外键的引用表不在当前可见范围内，未绘制连线。')
    if not edges:
        notices.append('当前范围没有可绘制的已声明外键；独立表仍会显示，不按字段名称猜测关联。')
    return dict(database=database, tables=list(nodes.values()), relationships=edges, truncated=truncated,
                omitted_references=omitted, notice=' '.join(notices), read_only=True)
