"""Schema extraction must parameterize identifiers and never invent relationships."""
import database_schema as graph


class Session:
    spec = dict(engine='postgres', database='example')

    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def query(self, sql, params, **kwargs):
        self.calls.append((sql, params))
        return next(self.results)


def result(rows, truncated=False):
    return dict(rows=rows, truncated=truncated)


def test_schema_filter_is_parameterized_and_independent_tables_are_preserved():
    session = Session([result([['public', 'odd"table', 'BASE TABLE']]),
                       result([['public', 'odd"table', 'customer_id', 'integer', 'YES']]), result([])])
    data = graph.relationships(session, "x' OR TRUE --")
    assert session.calls[0][1] == ("x' OR TRUE --",)
    assert 'OR TRUE' not in session.calls[0][0]
    assert session.calls[1][1] == ('public', 'odd"table')
    assert len(data['tables']) == 1 and not data['relationships']
    assert not data['tables'][0]['columns'][0]['foreign_key']


def test_partial_composite_foreign_key_is_not_rendered():
    session = Session([result([['public', 'child', 'BASE TABLE'], ['public', 'parent', 'BASE TABLE']]), result([]),
                       result([['FOREIGN KEY', 'composite', 'public', 'child', 'a', 1, 'public', 'parent', 'a', 'a', 'a']], True)])
    data = graph.relationships(session)
    assert data['truncated'] and data['relationships'] == []


def test_equal_constraint_names_on_distinct_tables_do_not_merge():
    session = Session([result([['public', 'a', 'BASE TABLE'], ['public', 'b', 'BASE TABLE']]), result([]),
                       result([['FOREIGN KEY', 'same_name', 'public', 'a', 'b_id', 1, 'public', 'b', 'id', 'c', 'r'],
                               ['FOREIGN KEY', 'same_name', 'public', 'b', 'a_id', 1, 'public', 'a', 'id', 'c', 'r']])])
    data = graph.relationships(session)
    assert len(data['relationships']) == 2
    assert data['relationships'][0]['columns'] == ['b_id']
    assert data['relationships'][1]['columns'] == ['a_id']
