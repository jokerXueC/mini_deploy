"""Database workflow, output escaping and responsive panel integration."""
import base64
import json

import pytest

from test_onboarding_browser import browser_page as browser_page, playwright


@pytest.fixture
def database_page(browser_page):
    page, _ = browser_page
    saved = dict(id='a' * 24, revision='b' * 24, name='只读业务库', engine='postgres', host='127.0.0.1',
                 port=5432, user='panel_reader', database='example', tls=False, ca_pem='', password_saved=True)
    state = dict(posts=[], secure=True, connections=[saved], cancelled=False, slow=False, jobs={})
    columns = ['id', 'name', 'total', 'optional']

    def api(route):
        path = route.request.url.split('/databases', 1)[-1].split('?')[0]
        payload = json.loads(route.request.post_data or '{}')
        if route.request.method == 'POST':
            state['posts'].append((path, payload))
        if path == '' and route.request.method == 'GET':
            result = dict(connections=state['connections'], dependencies=dict(postgres=dict(available=True),
                          mysql=dict(available=True), sql=True), csrf_token='test', secure=state['secure'], http_enabled=False)
        elif path == '/allow-http':
            state['secure'] = True
            result = dict(http_enabled=True)
        elif path == '' and payload.get('action') == 'save':
            state['connections'] = [{**saved, 'name': payload['name']}]
            result = dict(connection=state['connections'][0])
        elif path in ('/query', '/test'):
            operation = payload.get('operation', 'test')
            if operation == 'query' and not payload['sql'].strip().upper().startswith('SELECT'):
                route.fulfill(status=400, json=dict(detail='只读模式仅接受 SELECT'))
                return
            job = str(len(state['jobs']) + 1)
            if operation == 'catalog':
                value = dict(databases=['example'], database='example', tables=[dict(schema='public', name='orders', type='BASE TABLE')])
            elif operation == 'structure':
                value = dict(fields=dict(columns=['column_name', 'data_type'], rows=[['id', 'integer']]),
                             indexes=dict(columns=['indexname', 'indexdef'], rows=[['orders_pkey', 'CREATE UNIQUE INDEX orders_pkey ON orders(id)']]))
            elif operation == 'relationships':
                def field(name, primary=False, foreign=False):
                    return dict(name=name, type='integer', nullable=False, primary_key=primary, foreign_key=foreign)
                value = dict(database='example', tables=[
                    dict(id='t0', schema='public', name='orders', type='BASE TABLE', columns=[field('id', True), field('customer_id', foreign=True)]),
                    dict(id='t1', schema='public', name='customers', type='BASE TABLE', columns=[field('id', True), field('parent_id', foreign=True), field('<img src=x onerror=alert(1)>')]),
                    dict(id='t2', schema='public', name='independent', type='BASE TABLE', columns=[field('id', True)])],
                    relationships=[dict(id='f0', name='orders_customer', source='t0', target='t1', columns=['customer_id'],
                                        target_columns=['id'], update_rule='NO ACTION', delete_rule='RESTRICT'),
                                   dict(id='f1', name='customers_parent', source='t1', target='t1', columns=['parent_id'],
                                        target_columns=['id'], update_rule='NO ACTION', delete_rule='RESTRICT')], truncated=False, notice='')
            elif operation == 'test':
                value = dict(detail='连接成功，只读事务已建立。')
            else:
                value = dict(columns=columns, rows=[[payload.get('page', 0) * 50 + 1, '<img src=x onerror=alert(1)>', '1.25', None],
                                                   [2, '=SUM(1,2)', '2.50', 'a very long text ' * 40]],
                             has_more=payload.get('page', 0) == 0, sort=payload.get('sort') or 'id', truncated=False)
            value['access'] = dict(account=payload.get('user', saved['user']), transaction_read_only=True)
            state['jobs'][job] = value
            result = dict(id=job, state='running', elapsed_ms=10)
        elif path == '/job':
            job = route.request.url.split('id=')[1]
            result = dict(id=job, state='cancelled' if state['cancelled'] else 'running' if state['slow'] else 'succeeded',
                          error='查询已取消。' if state['cancelled'] else '', result=state['jobs'][job], elapsed_ms=12)
        elif path == '/cancel':
            state['cancelled'] = True
            result = dict(state='running')
        else:
            result = {}
        route.fulfill(json=result)

    page.route('**/databases', api)
    page.route('**/databases/*', api)
    page.locator('#databasesViewTab').click()
    playwright.expect(page.locator('#dbNew')).to_be_enabled()
    return page, state


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_database_data_structure_query_export_and_layout(database_page, tmp_path, width, theme):
    page, state = database_page
    page.set_viewport_size(dict(width=width, height=950))
    page.evaluate('(value) => document.documentElement.dataset.theme = value', theme)
    page.locator('#dbConnection').select_option('a' * 24)
    page.locator('#dbConnect').click()
    page.locator('[data-db-table]').click()
    playwright.expect(page.locator('#dbDataGrid')).to_contain_text('<img src=x onerror=alert(1)>')
    assert page.locator('#dbDataGrid img').count() == 0
    playwright.expect(page.locator('#dbDataGrid td[data-null=true]')).to_have_text('NULL')
    page.screenshot(path=str(tmp_path / f'database-{width}-{theme}.png'), full_page=True, animations='disabled')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#dbNext').click()
    playwright.expect(page.locator('#dbPage')).to_have_text('第 2 页')
    assert not page.locator('#dbNext').is_enabled()
    page.locator('#dbDataGrid td button').click()
    playwright.expect(page.locator('#dbCellContent')).to_contain_text('a very long text')
    page.keyboard.press('Escape')
    with page.expect_download() as download:
        page.locator('#dbDataExport').click()
    csv = download.value.path().read_text(encoding='utf-8-sig')
    assert "'=SUM(1,2)" in csv and 'optional' in csv
    page.locator('#dbStructureTab').click()
    playwright.expect(page.locator('#dbIndexesGrid')).to_contain_text('orders_pkey')
    page.locator('#dbSqlTab').click()
    page.locator('#dbSql').fill('DELETE FROM orders')
    page.locator('#dbRun').click()
    playwright.expect(page.locator('#dbSqlSummary')).to_contain_text('只读模式')
    page.locator('#dbSql').fill('SELECT * FROM public.orders LIMIT 10')
    page.locator('#dbRun').click()
    playwright.expect(page.locator('#dbSqlGrid')).to_contain_text('1.25')
    page.locator('#dbEdit').click()
    assert page.locator('#dbPassword').input_value() == ''
    page.screenshot(path=str(tmp_path / f'database-dialog-{width}-{theme}.png'), animations='disabled')
    assert page.locator('#dbConnectionDialog').evaluate('(el) => el.scrollWidth <= el.clientWidth')
    page.locator('#dbName').fill('改名后的只读库')
    page.locator('#dbSave').click()
    playwright.expect(page.locator('#dbConnectionDialog')).not_to_be_visible()
    assert state['posts'][-1][1]['password'] == ''


def test_database_cancel_running_query(database_page):
    page, state = database_page
    page.locator('#dbConnection').select_option('a' * 24)
    page.locator('#dbConnect').click()
    playwright.expect(page.locator('#dbConnectionState')).to_have_text('已连接')
    page.locator('#dbSqlTab').click()
    page.locator('#dbSql').fill('SELECT 1')
    state['slow'] = True
    page.locator('#dbRun').click()
    playwright.expect(page.locator('#dbFeedback')).to_have_attribute('data-running', 'true')
    page.locator('#dbCancel').click()
    playwright.expect(page.locator('#dbSqlSummary')).to_contain_text('已取消')
    playwright.expect(page.locator('#dbRun')).to_be_enabled()


def test_database_http_requires_own_consent(database_page):
    page, state = database_page
    state['secure'] = False
    page.locator('#refreshBtn').click()
    playwright.expect(page.locator('#dbHttpNote')).to_be_visible()
    playwright.expect(page.locator('#dbNew')).to_be_disabled()
    playwright.expect(page.locator('#dbAllowHttp')).to_be_disabled()
    page.locator('#dbHttpConsent').check()
    page.locator('#dbAllowHttp').click()
    playwright.expect(page.locator('#dbHttpNote')).to_be_hidden()
    playwright.expect(page.locator('#dbNew')).to_be_enabled()


def test_existing_root_account_can_be_tested_without_creating_a_new_user(database_page):
    page, state = database_page
    page.locator('#dbNew').click()
    page.locator('#dbName').fill('Existing database')
    page.locator('#dbUser').fill('root')
    page.locator('#dbPassword').fill('fixture-password')
    page.locator('#dbTest').click()
    playwright.expect(page.locator('#dbConnectionFeedback')).to_contain_text('当前账号：root；只读事务已验证')
    assert state['posts'][-1][0] == '/test' and state['posts'][-1][1]['user'] == 'root'
    assert all(payload.get('action') not in {'create_user', 'grant'} for _, payload in state['posts'])


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_database_relationship_view_canvas_controls_and_table_navigation(database_page, tmp_path, width, theme):
    page, state = database_page
    page.set_viewport_size(dict(width=width, height=950))
    page.evaluate('(value) => applyTheme(value)', theme)
    page.locator('#dbConnection').select_option('a' * 24)
    page.locator('#dbConnect').click()
    playwright.expect(page.locator('#dbConnectionState')).to_have_text('已连接')
    page.locator('#dbGraphMode').click()
    playwright.expect(page.locator('#dbGraphCards .db-graph-card')).to_have_count(3)
    playwright.expect(page.locator('#dbGraphCount')).to_have_text('3 张表 / 2 条外键')
    assert page.evaluate('''() => {
      const edge = document.querySelector('#dbGraphCanvas')._cyreg.cy.getElementById('f1');
      const box = edge.boundingBox(), start = edge.sourceEndpoint(), end = edge.targetEndpoint();
      return box.w > 0 && box.h > 0 && [start.x,start.y,end.x,end.y].every(Number.isFinite);
    }''')
    assert page.locator('#dbGraphCards img').count() == 0
    assert page.locator('#dbGraphStage canvas').count() > 0
    assert page.evaluate('''() => [...document.querySelectorAll('#dbGraphCanvas canvas')].some(canvas => {
      const context = canvas.getContext('2d'); if (!context) return false;
      const data = context.getImageData(0,0,canvas.width,canvas.height).data;
      let nonblank = 0; for (let i=3;i<data.length;i+=4) if(data[i]) nonblank++;
      return nonblank > 100;
    })''')
    page.screenshot(path=str(tmp_path / f'database-relations-{width}-{theme}.png'), full_page=True, animations='disabled')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#dbGraphStage').scroll_into_view_if_needed()
    first_card = page.locator('[data-graph-table=t0]')
    initial_left = first_card.evaluate('(el)=>el.style.left')
    box = first_card.bounding_box()
    page.mouse.move(box['x'] + box['width'] / 2, box['y'] + min(12, box['height'] / 4))
    page.mouse.down()
    page.mouse.move(box['x'] + box['width'] / 2 + 45, box['y'] + 35, steps=6)
    page.mouse.up()
    assert first_card.evaluate('(el)=>el.style.left') != initial_left
    before = page.locator('#dbGraphScale').inner_text()
    page.locator('#dbGraphZoomIn').click()
    assert page.locator('#dbGraphScale').inner_text() != before
    page.locator('#dbGraphFit').click()
    page.locator('#dbGraphConnected').check()
    playwright.expect(page.locator('[data-graph-table=t2]')).to_be_hidden()
    page.locator('#dbGraphConnected').uncheck()
    page.locator('#dbGraphTable').select_option('t0')
    playwright.expect(page.locator('#dbGraphDetails h3')).to_have_text('orders')
    page.locator('#dbGraphDetails').get_by_role('button', name='orders_customer', exact=True).click()
    playwright.expect(page.locator('#dbGraphDetails')).to_contain_text('public.orders.customer_id')
    playwright.expect(page.locator('#dbGraphDetails')).to_contain_text('public.customers.id')
    page.locator('#dbGraphSearch').fill('customers')
    page.locator('#dbGraphSearch').press('Enter')
    playwright.expect(page.locator('#dbGraphDetails h3')).to_have_text('customers')
    page.locator('#dbGraphExpand').click()
    playwright.expect(page.locator('#dbGraphModal')).to_be_visible()
    assert page.locator('#dbGraphStage').evaluate('(el)=>el.clientWidth > 200 && el.clientHeight > 200')
    page.screenshot(path=str(tmp_path / f'database-relations-expanded-{width}-{theme}.png'), animations='disabled')
    with page.expect_download() as download:
        page.locator('#dbGraphExport').click()
    png = download.value.path().read_bytes()
    assert png.startswith(b'\x89PNG')
    # The exported graph must include readable foreground text, not only table outlines and edges.
    assert page.evaluate('''async data => {
      const image = new Image(); image.src = 'data:image/png;base64,' + data; await image.decode();
      const canvas = document.createElement('canvas'); canvas.width=image.width; canvas.height=image.height;
      const context = canvas.getContext('2d');
      context.fillStyle=getComputedStyle(document.querySelector('#dbGraphPane')).getPropertyValue('--fg');
      context.fillRect(0,0,1,1); const foreground=context.getImageData(0,0,1,1).data;
      context.drawImage(image,0,0); const pixels=context.getImageData(0,0,canvas.width,canvas.height).data;
      let text=0; for(let i=0;i<pixels.length;i+=4) {
        if(pixels[i+3]===255 && [0,1,2].every(c => Math.abs(pixels[i+c]-foreground[c]) < 25)) text++;
      }
      return text > 300;
    }''', base64.b64encode(png).decode('ascii'))
    page.locator('#dbGraphClose').click()
    playwright.expect(page.locator('#dbGraphModal')).to_be_hidden()
    page.locator('#dbGraphDetails').get_by_role('button', name='查看数据', exact=True).click()
    playwright.expect(page.locator('#dbBrowseView')).to_be_visible()
    playwright.expect(page.locator('#dbObjectTitle')).to_have_text('customers')
    playwright.expect(page.locator('#dbDataGrid')).to_contain_text('1.25')
    assert any(payload.get('operation') == 'relationships' for _, payload in state['posts'])
