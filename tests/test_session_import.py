import asyncio
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

# Import the application only against throwaway storage, without running startup.
_SANDBOX = tempfile.TemporaryDirectory()
# WARMUP_ENABLED=0：测试里的账号都是凭空造的、一条都没发成功过，
# 开着预热门禁会把它们全判成「预热中」而拦住，和这些用例要测的东西无关。
# 门禁本身由 WarmupTierTests 显式打开来测。
os.environ.update(ADMIN_TOKEN='test-only', DB_URL='sqlite:///' + _SANDBOX.name + '/db.sqlite', SESSION_DIR=_SANDBOX.name + '/sessions', RESUME_TASKS_ON_STARTUP='0', WARMUP_ENABLED='0')
import httpx
from telethon.sessions import SQLiteSession
from telethon.crypto import AuthKey
import main
main.Base.metadata.create_all(main.engine)
from app.services.proxy_service import parse_proxy_input, normalize_proxy_input, resolve_proxy_source_text, assign_proxy_pool_to_accounts


class ImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = main.CONFIG.SESSION_DIR
        main.CONFIG.SESSION_DIR = self.tmp.name
        session = SQLiteSession(str(Path(self.tmp.name) / 'fixture'))
        session.set_dc(2, '149.154.167.51', 443)
        session.auth_key = AuthKey(bytes(range(256)))
        session.save()
        session.close()
        self.data = (Path(self.tmp.name) / 'fixture.session').read_bytes()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        main.CONFIG.SESSION_DIR = self.previous
        self.tmp.cleanup()

    async def upload(self, files, proxy='http://127.0.0.1:8080'):
        return await self.client.post('/api/accounts/upload-sessions', headers={'X-Admin-Token': 'test-only'}, files=files, data={'proxy_source_text': proxy})

    async def test_valid_and_duplicate_preserves_file(self):
        response = await self.upload([('files', ('new.session', self.data))])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['uploaded'], 1)
        self.assertEqual(response.json()['proxy_assigned'], 1)
        target = Path(self.tmp.name) / 'new.session'
        before = target.read_bytes()
        # 重复导入不再整批失败：200 + duplicate 计数，原文件必须原样保留
        response = await self.upload([('files', ('new.session', self.data))])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['duplicate'], 1)
        self.assertEqual(response.json()['uploaded'], 0)
        self.assertEqual(target.read_bytes(), before)

    async def test_invalid_file_not_saved(self):
        response = await self.upload([('files', ('broken.session', b'X' * 10000))])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['invalid'], 1)
        self.assertEqual(response.json()['uploaded'], 0)
        self.assertFalse((Path(self.tmp.name) / 'broken.session').exists())

    async def test_import_without_proxy(self):
        """代理不是上传的前提：没有代理也必须导入成功，并标记为 NO_PROXY。"""
        response = await self.upload([('files', ('noproxy.session', self.data))], '')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['uploaded'], 1)
        self.assertEqual(body['no_proxy'], ['noproxy'])
        self.assertEqual(body['no_proxy_count'], 1)
        self.assertEqual(body['proxy_assigned'], 0)
        self.assertTrue((Path(self.tmp.name) / 'noproxy.session').exists())

    async def test_dashboard_overview_shape(self):
        """聚合接口：只导入未检测的账号时必须计入 unchecked，不能算成异常。"""
        await self.upload([('files', ('ov1.session', self.data))], '')
        response = await self.client.get('/api/dashboard/overview', headers={'X-Admin-Token': 'test-only'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['accounts']['total'], 2, body)
        self.assertEqual(body['accounts']['unchecked'], 2, body)
        self.assertEqual(body['accounts']['error'], 0, body)
        self.assertEqual(body['accounts']['ready'], 0, body)
        self.assertEqual(body['scheduler']['status'], 'IDLE', body)
        # 群组池计数必须在聚合接口里，前端 5 秒轮询就不用重复拉整个池子
        self.assertEqual(body['groups']['total'], 0, body)

    async def test_proxy_enforce_switch_blocks_and_unblocks(self):
        """开关是可选开关：默认关闭=无代理可上传；打开后同一次上传必须整批拒绝；关回去又能传。"""
        policy_url = '/api/settings/proxy-policy'
        headers = {'X-Admin-Token': 'test-only'}

        default = await self.client.get(policy_url, headers=headers)
        self.assertEqual(default.status_code, 200, default.text)
        self.assertFalse(default.json()['enforce_one_to_one'])

        on = await self.client.post(policy_url, headers=headers, json={'enforce_one_to_one': True})
        self.assertEqual(on.status_code, 200, on.text)
        self.assertTrue(on.json()['enforce_one_to_one'])
        try:
            response = await self.upload([('files', ('blocked.session', self.data))], '')
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()['detail'], 'proxy_required_per_account')
            self.assertFalse((Path(self.tmp.name) / 'blocked.session').exists())
        finally:
            # 断言失败也必须关回去，别把开关状态泄漏给其它用例
            await self.client.post(policy_url, headers=headers, json={'enforce_one_to_one': False})

        response = await self.upload([('files', ('allowed.session', self.data))], '')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['uploaded'], 1)
        self.assertTrue((Path(self.tmp.name) / 'allowed.session').exists())

    async def test_zip_import_without_proxy(self):
        """ZIP 批量导入且无代理：账号必须全部落盘，代理不足不允许再拦整批。"""
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('nested/a.session', self.data)
            z.writestr('nested/b.session', self.data)
        response = await self.upload([('files', ('batch.zip', archive.getvalue()))], '')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['uploaded'], 2)
        self.assertEqual(response.json()['no_proxy_count'], 2)
        self.assertTrue((Path(self.tmp.name) / 'a.session').exists())
        self.assertTrue((Path(self.tmp.name) / 'b.session').exists())

    async def test_zip_import_with_pool(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('nested/c.session', self.data)
            z.writestr('nested/d.session', self.data)
        response = await self.upload(
            [('files', ('batch2.zip', archive.getvalue()))],
            'http://127.0.0.1:8080\nsocks5://127.0.0.1:8081',
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['uploaded'], 2)
        self.assertEqual(response.json()['proxy_assigned'], 2)

    async def test_failed_binding_does_not_publish_session(self):
        """原子性不放宽：绑定写库失败时 session 不能发布，但整批仍是 200 + 计入 invalid。"""
        with patch.object(main, 'upsert_proxy_binding', side_effect=RuntimeError('database unavailable')):
            response = await self.upload([('files', ('dbfail.session', self.data))])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['invalid'], 1)
        self.assertEqual(response.json()['uploaded'], 0)
        self.assertFalse((Path(self.tmp.name) / 'dbfail.session').exists())

    async def test_partial_import_keeps_original_proxy_mapping(self):
        response = await self.upload([('files', ('bad.session', b'broken')), ('files', ('good.session', self.data))], 'http://127.0.0.1:9001\nhttp://127.0.0.1:9002')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['validated_accounts'], ['good'])
        with main.SessionLocal() as db:
            binding = main.list_proxy_bindings(db, ['good'])['good']
            self.assertEqual(binding['port'], 9002)

    async def test_legacy_session_migration(self):
        import sqlite3
        path = Path(self.tmp.name) / 'fixture.session'
        with sqlite3.connect(path) as db:
            cols = {r[1] for r in db.execute('PRAGMA table_info(sessions)')}
            if 'tmp_auth_key' in cols:
                db.execute('ALTER TABLE sessions DROP COLUMN tmp_auth_key')
            db.execute('UPDATE version SET version=7')
        response = await self.upload([('files', ('legacy.session', path.read_bytes()))])
        self.assertEqual(response.status_code, 200, response.text)

    async def test_purge_sessions_removes_every_session_file(self):
        """清空 session：主文件连同 journal/wal/shm 残留一起删，账号列表归零。"""
        from app.database import SessionLocal
        from app.models import AccountProxy, SystemKV

        (Path(self.tmp.name) / 'fixture.session').unlink()  # setUp 留的样例文件不算账号
        await self.upload([('files', ('purge_me.session', self.data))])
        for suffix in ('.session-journal', '.session-wal', '.session-shm'):
            (Path(self.tmp.name) / f'purge_me{suffix}').write_bytes(b'x')
        db = SessionLocal()
        try:
            db.add(SystemKV(k='account_status:purge_me', v='{"status": "unauthorized", "valid": false}'))
            db.commit()
        finally:
            db.close()

        response = await self.client.post('/api/accounts/purge-sessions',
                                          headers={'X-Admin-Token': 'test-only'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['accounts'], 1, body)
        self.assertEqual(body['files_removed'], 4, body)
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()), [], body)

        db = SessionLocal()
        try:
            self.assertEqual(
                db.query(AccountProxy).filter(AccountProxy.account_name == 'purge_me').count(), 0,
                '代理绑定要跟着账号一起清')
            self.assertIsNone(
                db.query(SystemKV).filter(SystemKV.k == 'account_status:purge_me').first(),
                '上一次的检测结果不能留着，否则重传同一个号会读到旧状态')
        finally:
            db.close()

        listed = await self.client.get('/api/accounts/status', headers={'X-Admin-Token': 'test-only'})
        left = [r['account'] for r in listed.json() if r['status'] != 'missing_file']
        self.assertEqual(left, [], listed.text)

    async def test_purge_on_empty_dir_is_not_an_error(self):
        (Path(self.tmp.name) / 'fixture.session').unlink()
        response = await self.client.post('/api/accounts/purge-sessions',
                                          headers={'X-Admin-Token': 'test-only'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['accounts'], 0, response.text)

    async def test_unauthorized(self):
        response = await self.client.post('/api/accounts/upload-sessions')
        self.assertEqual(response.status_code, 401)


class ProxyTests(unittest.TestCase):
    def test_http_endpoint_is_not_fetched(self):
        with patch('app.services.proxy_service.urlopen', side_effect=AssertionError('unexpected network')):
            errors = []
            _, pool, meta = resolve_proxy_source_text('http://user:pass@localhost:8080', errors)
            self.assertEqual(pool, ['http://user:pass@localhost:8080'])
            self.assertEqual(errors, [])
            self.assertEqual(meta['remote_sources'], 0)

    def test_password_roundtrip(self):
        raw = 'socks5://user:p%40ss%3Aword%23@localhost:1080'
        self.assertEqual(parse_proxy_input(raw)['password'], 'p@ss:word#')
        self.assertEqual(normalize_proxy_input(raw), raw)

    def test_no_duplicate_explicit_or_pool(self):
        p = 'http://localhost:8080'
        result, meta = assign_proxy_pool_to_accounts(['a','b','c'], {'a':p, 'b':p}, [p])
        self.assertEqual(result, {'a':p})
        self.assertEqual(meta['unassigned_accounts'], ['b','c'])

    def test_sticky_derivation_for_single_gateway(self):
        """1 个轮换型网关 + N 个账号：必须派生出 N 个互不相同的粘性会话。"""
        gateway = 'http://USER-zone-custom:pass@gateway.test:10000'
        accounts = ['111', '222', '333']
        result, meta = assign_proxy_pool_to_accounts(accounts, {}, [gateway], allow_sticky=True)
        self.assertEqual(meta['assigned_from_sticky'], 3)
        self.assertEqual(len(result), 3)
        self.assertEqual(len(set(result.values())), 3)
        for account in accounts:
            self.assertIn('-session-' + account, result[account])

    def test_static_pool_without_username_binds_one_to_one(self):
        """无认证的固定 IP 代理派生不出粘性会话：池子不够就一一对应，能绑几个绑几个。"""
        result, meta = assign_proxy_pool_to_accounts(
            ['a', 'b'], {}, ['http://gateway.test:10000'], allow_sticky=True,
        )
        self.assertEqual(result, {'a': 'http://gateway.test:10000'})
        self.assertEqual(meta['unassigned_accounts'], ['b'])

    def test_already_bound_proxy_is_not_given_to_another_account(self):
        """补绑时排除已绑出去的代理：同一个 IP 不能挂两个号。"""
        from app.services.proxy_service import assign_proxies_from_source

        p = 'http://gateway.test:10000'
        with patch('app.services.proxy_service.resolve_proxy_source_text',
                   return_value=({}, [p], {})):
            result = assign_proxies_from_source(['new_acct'], p, exclude_proxies=[p])
        self.assertEqual(result['assigned_total'], 0)
        self.assertEqual(result['assignment_meta']['unassigned_accounts'], ['new_acct'])

    def test_provider_url_is_fetched(self):
        with patch('app.services.proxy_service.fetch_proxy_lines_from_url', return_value=['http://localhost:8080']) as fetch:
            _, pool, _ = resolve_proxy_source_text('https://example.test/get?proto=http')
            fetch.assert_called_once()
            self.assertEqual(pool, ['http://localhost:8080'])


class GroupPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.database import SessionLocal
        from app.models import TargetGroup
        self.SessionLocal = SessionLocal
        self.TargetGroup = TargetGroup
        db = SessionLocal()
        try:
            db.query(TargetGroup).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
        self.headers = {'X-Admin-Token': 'test-only'}
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_normalize_accepts_all_documented_formats(self):
        from app.routers.group_pool import normalize_group_line
        cases = {
            'https://t.me/somegroup': '@somegroup',
            't.me/somegroup': '@somegroup',
            '@somegroup': '@somegroup',
            'somegroup': '@somegroup',
            'https://t.me/somegroup/123': '@somegroup',
            'https://t.me/+AbCdEf12345': '+AbCdEf12345',
            'https://t.me/joinchat/AbCdEf12345': '+AbCdEf12345',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                entry = normalize_group_line(raw)
                self.assertIsNotNone(entry, raw)
                self.assertEqual(entry['username'], expected)

    async def test_normalize_rejects_garbage(self):
        from app.routers.group_pool import normalize_group_line
        for raw in ['', '   ', '# 注释', 'https://t.me/c/1234/5', 'ab', 'not a group name!']:
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_group_line(raw))

    async def test_import_partial_success(self):
        """500 条里混坏行：不能整批失败，成功/重复/无效必须分别计数。"""
        text = '\n'.join([
            'https://t.me/alpha', '@beta', 'gamma', 'https://t.me/+InviteHash01',
            'alpha',            # 批内重复
            'ab',               # 无效
            '',                 # 空行
        ])
        response = await self.client.post('/api/groups/import', headers=self.headers, json={'text': text})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['imported'], 4, body)
        self.assertEqual(body['duplicate'], 1, body)
        self.assertEqual(body['invalid'], 1, body)
        self.assertEqual(body['total'], 6, body)

        # 再导一次同样的内容：4 条全部记为重复，不新增
        again = (await self.client.post('/api/groups/import', headers=self.headers, json={'text': text})).json()
        self.assertEqual(again['imported'], 0, again)
        self.assertEqual(again['duplicate'], 5, again)

    async def test_import_from_file_and_list(self):
        payload = 'https://t.me/fromfile\nt.me/second\n'.encode()
        response = await self.client.post(
            '/api/groups/import', headers=self.headers,
            files={'file': ('groups.txt', payload, 'text/plain')},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['imported'], 2, response.text)

        listing = (await self.client.get('/api/groups/pool', headers=self.headers)).json()
        self.assertEqual(listing['total'], 2, listing)
        self.assertEqual(listing['by_status'], {'pending': 2}, listing)
        self.assertEqual({item['link'] for item in listing['items']},
                         {'https://t.me/fromfile', 'https://t.me/second'})

    async def test_delete_by_ids_and_scope(self):
        await self.client.post('/api/groups/import', headers=self.headers,
                               json={'text': 'https://t.me/keep\nhttps://t.me/drop'})
        listing = (await self.client.get('/api/groups/pool', headers=self.headers)).json()
        drop_id = next(item['id'] for item in listing['items'] if item['username'] == '@drop')

        response = await self.client.post('/api/groups/pool/delete', headers=self.headers, json={'ids': [drop_id]})
        self.assertEqual(response.json()['deleted'], 1, response.text)
        remaining = (await self.client.get('/api/groups/pool', headers=self.headers)).json()
        self.assertEqual([item['username'] for item in remaining['items']], ['@keep'])

        response = await self.client.post('/api/groups/pool/delete', headers=self.headers, json={'scope': 'all'})
        self.assertEqual(response.json()['deleted'], 1, response.text)
        self.assertEqual((await self.client.get('/api/groups/pool', headers=self.headers)).json()['total'], 0)


class PoolRecheckTests(unittest.IsolatedAsyncioTestCase):
    """群池重检：没有可用账号时不去连 Telegram，已缓存 id 也要重新确认。"""

    async def asyncSetUp(self):
        from app.database import SessionLocal
        from app.models import TargetGroup, Task, SystemKV
        self.SessionLocal = SessionLocal
        self.TargetGroup = TargetGroup
        self.Task = Task
        self.SystemKV = SystemKV
        db = SessionLocal()
        try:
            for table in (TargetGroup, Task, SystemKV):
                db.query(table).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
        self.headers = {'X-Admin-Token': 'test-only'}
        # 限流状态是模块级的，测试之间必须清干净，否则会互相 429
        main._LAST_TS.clear()
        main._REQ_IDS.clear()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')

    async def asyncTearDown(self):
        for name in ('acct_a', 'acct_b', 'acct_c'):
            main.account_service._cache.pop((name, False), None)
            main.account_service._cache.pop((name, True), None)
        await self.client.aclose()

    def mark_ready(self, name):
        import time
        main.account_service._cache[(name, False)] = (time.time(), {'status': 'ok', 'valid': True})

    def add_groups(self, pairs, members=('acct_a', 'acct_b', 'acct_c')):
        """pairs: [(username, group_id)]，group_id 已给定时不触发解析。

        members 是该群已知的成员账号：只有已加入该群的账号才能发这个群。
        """
        import json
        db = self.SessionLocal()
        try:
            for username, gid in pairs:
                db.add(self.TargetGroup(username=username, link=f'https://t.me/{username.lstrip("@")}',
                                        kind='public', group_id=gid, status='available',
                                        joined_by_json=json.dumps(list(members))))
            db.commit()
        finally:
            db.close()

    async def test_pool_recheck_needs_a_ready_account(self):
        """没有可用账号时不去连 Telegram，直接告诉用户先去检测账号。"""
        self.add_groups([('@c1', -4001)])
        with patch('main._discover_session_accounts', return_value=['acct_a']):
            response = await self.client.post('/api/groups/pool/check', headers=self.headers, json={})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()['detail'], 'no_ready_accounts', response.text)

    async def test_pool_recheck_forces_re_resolve(self):
        """已缓存 id 的群也要重新确认；这次失败就把状态与 id 一起清掉。"""
        self.mark_ready('acct_a')
        self.add_groups([('@c2', -4002)])
        with patch('main._discover_session_accounts', return_value=['acct_a']), \
             patch('app.services.plan_service.multi_manager.resolve_group_ref',
                   return_value={'ok': False, 'error': 'username_not_occupied'}):
            response = await self.client.post('/api/groups/pool/check', headers=self.headers, json={})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body['checked'], body['available'], body['unreachable']), (1, 0, 1), body)

        db = self.SessionLocal()
        try:
            group = db.query(self.TargetGroup).filter_by(username='@c2').one()
            self.assertIsNone(group.group_id, group.group_id)
            self.assertEqual(group.status, 'unreachable', group.status)
        finally:
            db.close()

class PoolJoinTests(unittest.IsolatedAsyncioTestCase):
    """批量加群：群组平均分给可用账号，同一个群不重复分配、不重复加入。"""

    async def asyncSetUp(self):
        from app.database import SessionLocal
        from app.models import GroupCache, JoinLog, TargetGroup
        from app.services import join_service
        self.SessionLocal = SessionLocal
        self.TargetGroup = TargetGroup
        self.GroupCache = GroupCache
        self.JoinLog = JoinLog
        # 全局加群令牌桶是给真实 IP 限流用的，测试里关掉，不然每个群都要等 3 秒
        self._no_throttle = patch.object(join_service, 'JOIN_BURST', 0)
        self._no_throttle.start()
        db = SessionLocal()
        try:
            db.query(TargetGroup).delete(synchronize_session=False)
            db.query(GroupCache).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
        self.headers = {'X-Admin-Token': 'test-only'}
        main._LAST_TS.clear()
        main._REQ_IDS.clear()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')

    async def asyncTearDown(self):
        self._no_throttle.stop()
        for name in ('acct_a', 'acct_b', 'acct_c'):
            for flag in (False, True):
                main.account_service._cache.pop((name, flag), None)
        await self.client.aclose()

    def mark_ready(self, name, status='ok'):
        import time
        main.account_service._cache[(name, False)] = (time.time(), {'status': status, 'valid': True})

    def add_groups(self, pairs, members=()):
        import json
        db = self.SessionLocal()
        try:
            for username, gid in pairs:
                db.add(self.TargetGroup(username=username, link=f'https://t.me/{username.lstrip("@")}',
                                        kind='public', group_id=gid, status='available',
                                        joined_by_json=json.dumps(list(members))))
            db.commit()
        finally:
            db.close()

    async def test_cannot_send_account_still_can_join(self):
        """「无群发言权限」多半是一个群都没加，没有群可测；加群这条链路不该把它挡掉。"""
        self.mark_ready('acct_a', status='cannot_send')
        self.add_groups([('@m1', -5051)])
        response = await self.client.post('/api/groups/pool/join', headers=self.headers,
                                          json={'accounts': ['acct_a']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['accounts_used'], 1, response.text)

    async def test_preview_distributes_evenly(self):
        self.mark_ready('acct_a')
        self.mark_ready('acct_b')
        self.add_groups([(f'@j{i}', -5000 - i) for i in range(7)])
        response = await self.client.post('/api/groups/pool/join', headers=self.headers,
                                          json={'accounts': ['acct_a', 'acct_b', 'acct_c']})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body['started'], body)
        self.assertEqual((body['accounts_used'], body['groups']), (2, 7), body)
        counts = sorted(item['count'] for item in body['per_account'])
        self.assertEqual(counts, [3, 4], body)
        self.assertEqual(body['accounts_blocked'], {'尚未检测登录状态': 1}, body)

    async def test_preview_needs_a_ready_account(self):
        self.add_groups([('@k1', -5101)])
        response = await self.client.post('/api/groups/pool/join', headers=self.headers,
                                          json={'accounts': ['acct_a']})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()['detail'], 'no_ready_accounts', response.text)

    async def test_stale_check_is_not_trusted(self):
        """检测结果过期就不再当可用：8002 就是拿 16 分钟前的快照去跑了 155 个群。"""
        import time
        from app.services.plan_service import STATUS_TRUST_SECONDS

        main.account_service._cache[('acct_stale', False)] = (
            time.time() - STATUS_TRUST_SECONDS - 1, {'status': 'ok', 'valid': True})
        self.add_groups([('@s1', -5501)])
        try:
            response = await self.client.post('/api/groups/pool/join', headers=self.headers,
                                              json={'accounts': ['acct_stale']})
        finally:
            main.account_service._cache.pop(('acct_stale', False), None)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()['blocked_reasons'], {'检测结果已过期，需重新检测': 1}, response.text)

    async def test_manual_join_tops_each_group_up_to_two_members(self):
        """每个群要配 2 个成员当备胎：只有 1 个成员的群还要再补 1 个，配够的才跳过。

        但绝不会把群再分给已经在里面的账号 —— 那只会被加群服务记成「已跳过」。
        """
        self.mark_ready('acct_a')
        self.mark_ready('acct_b')
        self.add_groups([('@half', -5301)], members=('acct_a',))
        self.add_groups([('@full', -5304)], members=('acct_a', 'acct_b'))
        self.add_groups([('@empty', -5302)])

        response = await self.client.post('/api/groups/pool/join', headers=self.headers,
                                          json={'accounts': ['acct_a', 'acct_b']})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['groups'], 2, body)                 # @half + @empty
        self.assertEqual(body['groups_covered_skipped'], 1, body)  # @full
        by_account = {item['account']: item['sample'] for item in body['per_account']}
        self.assertNotIn('@half', by_account.get('acct_a', []), body)
        self.assertIn('@half', [g for s in by_account.values() for g in s], body)

    async def test_manual_join_refuses_when_pool_is_fully_covered(self):
        """全池成员都配够时明确说原因，不要起一轮只是「已跳过」的空跑。"""
        self.mark_ready('acct_a')
        self.mark_ready('acct_b')
        self.add_groups([('@taken', -5303)], members=('acct_a', 'acct_b'))

        response = await self.client.post('/api/groups/pool/join', headers=self.headers,
                                          json={'accounts': ['acct_a', 'acct_b'], 'mode': 'start'})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()['detail'], 'all_groups_covered', response.text)

    async def test_dead_account_stops_the_rest_of_its_groups(self):
        """撞到 401 就是账号掉线：剩下的群不用再试，且当场把账号判为失效。

        掉线账号名下的群会被交给别的账号接手；没有别的账号时才算这些群失败。
        """
        from app.services import join_service
        from unittest.mock import AsyncMock

        self.add_groups([(f'@d{i}', -5400 - i) for i in range(5)])
        groups = self._groups()

        account = 'acct_dead'
        main.account_service._cache.pop((account, False), None)
        assignment = join_service.build_assignment(groups, [account])
        join_mock = AsyncMock(return_value={
            'ok': False,
            'error': 'join_public_failed: The key is not registered in the system (caused by ResolveUsernameRequest)',
        })
        try:
            with patch('app.services.join_service.multi_manager.get_joined_groups',
                       new=AsyncMock(return_value=[])), \
                 patch('app.services.join_service.multi_manager.join_group', new=join_mock):
                job = join_service._create_job(assignment, 0, 1)
                await join_service._run_job(job)

            self.assertEqual(join_mock.await_count, 1, '账号已经掉线，不该再拿剩下的群去试')
            self.assertEqual(job['failed'], 5, job)
            self.assertEqual(job['joined'], 0, job)
            self.assertTrue(all('登录已失效' in (item['error'] or '') for item in job['items']), job['items'])
            self.assertEqual(main.account_service.cached_status(account)['status'], 'unauthorized')
        finally:
            main.account_service._cache.pop((account, False), None)
            from app.models import SystemKV
            db = self.SessionLocal()
            try:
                db.query(SystemKV).filter(SystemKV.k == 'account_status:acct_dead').delete(synchronize_session=False)
                db.commit()
            finally:
                db.close()

    async def test_join_backfills_group_id_for_newly_imported_links(self):
        """刚导入的链接 group_id 为空：加群时回传的 id 必须回填，否则永远进不了发送列表。"""
        import json
        from app.services import join_service
        from unittest.mock import AsyncMock

        db = self.SessionLocal()
        try:
            row = self.TargetGroup(username='@brandnew', link='https://t.me/brandnew',
                                   kind='public', group_id=None, status='pending')
            db.add(row)
            db.commit()
            row_id = row.id
        finally:
            db.close()

        assignment = join_service.build_assignment(self._groups(), ['acct_a'])
        join_mock = AsyncMock(return_value={'ok': True, 'group_id': -1001234567890, 'title': 'brandnew'})
        with patch('app.services.join_service.multi_manager.get_joined_groups',
                   new=AsyncMock(return_value=[])), \
             patch('app.services.join_service.multi_manager.join_group', new=join_mock):
            job = join_service._create_job(assignment, 0, 1)
            await join_service._run_job(job)

        self.assertEqual(job['joined'], 1, job)
        db = self.SessionLocal()
        try:
            row = db.query(self.TargetGroup).filter(self.TargetGroup.id == row_id).first()
            self.assertEqual(row.group_id, -1001234567890)
            self.assertEqual(row.status, 'available')
            self.assertIn('acct_a', json.loads(row.joined_by_json or '[]'))
        finally:
            db.close()

    def _groups(self):
        db = self.SessionLocal()
        try:
            return db.query(self.TargetGroup).order_by(self.TargetGroup.id.asc()).all()
        finally:
            db.close()

    def _joined_by(self, group_id):
        import json
        db = self.SessionLocal()
        try:
            row = db.query(self.TargetGroup).filter(self.TargetGroup.group_id == group_id).first()
            return json.loads(row.joined_by_json or '[]')
        finally:
            db.close()

    async def _run_join_job(self, accounts, assign_to, outcomes):
        """跑一轮真实调度：assignment 只分给 assign_to，但可用账号池是 accounts。"""
        from unittest.mock import AsyncMock
        from app.services import join_service

        assignment = join_service.build_assignment(self._groups(), assign_to)
        join_mock = AsyncMock(side_effect=outcomes)
        with patch('app.services.join_service.multi_manager.get_joined_groups',
                   new=AsyncMock(return_value=[])), \
             patch('app.services.join_service.multi_manager.join_group', new=join_mock):
            job = join_service._create_job(assignment, 0, 1, accounts=accounts)
            await join_service._run_job(job)
        return job, join_mock

    async def test_group_a_cannot_join_is_handed_to_b(self):
        """A 加不上不代表这个群废了：换 B 去加，群发就多一个能用的目标群。"""
        self.add_groups([('@hand', -5601)])
        self.mark_ready('acct_a')
        self.mark_ready('acct_b')

        def outcomes(account, username, known_joined_ids=None):
            if account == 'acct_a':
                return {'ok': False, 'error': 'join_public_failed: something odd'}
            return {'ok': True, 'group_id': -5601, 'title': 'hand'}

        job, join_mock = await self._run_join_job(['acct_a', 'acct_b'], ['acct_a'], outcomes)

        self.assertEqual(job['joined'], 1, job['items'])
        self.assertEqual(job['failed'], 0, job['items'])
        self.assertEqual(job['redispatches'], 1, job)
        self.assertEqual(job['items'][0]['account'], 'acct_b', job['items'][0])
        self.assertEqual(job['items'][0]['tried'], ['acct_a', 'acct_b'], job['items'][0])
        self.assertEqual(join_mock.await_count, 2, job['items'])
        # 成员关系要落到 B 名下：发送链路只认「谁在这个群里」
        self.assertEqual(self._joined_by(-5601), ['acct_b'])

    async def test_dead_group_is_not_retried_with_another_account(self):
        """群/链接本身没用了，换号也是同一句：不许再拿别的账号去试。"""
        self.add_groups([('@gone', -5602)])
        self.mark_ready('acct_a')
        self.mark_ready('acct_b')

        async def outcomes(account, username, known_joined_ids=None):
            return {'ok': False,
                    'error': 'join_public_failed: Cannot find any entity corresponding to "gone"'}

        job, join_mock = await self._run_join_job(['acct_a', 'acct_b'], ['acct_a'], outcomes)

        self.assertEqual(join_mock.await_count, 1, '群已经废了，不该换号再试')
        self.assertEqual(job['redispatches'], 0, job)
        self.assertEqual(job['failed'], 1, job['items'])
        self.assertIn('群不可用', job['items'][0]['error'], job['items'][0])
        # 状态要落库：否则链路每 30 秒都会拿这个死群重试一次
        db = self.SessionLocal()
        try:
            row = db.query(self.TargetGroup).filter_by(username='@gone').one()
            self.assertEqual(row.status, 'unreachable', row.status)
        finally:
            db.close()

    async def test_account_at_channel_limit_quits_joining_without_being_killed(self):
        """账号加群额度满了：它退出加群，但还能发消息，绝不能被判成失效。"""
        self.add_groups([(f'@lim{i}', -5700 - i) for i in range(3)])
        self.mark_ready('acct_a')
        self.mark_ready('acct_b')

        def outcomes(account, username, known_joined_ids=None):
            if account == 'acct_a':
                return {'ok': False,
                        'error': 'join_public_failed: You have joined too many channels/supergroups'}
            # 加群顺序已随机化：返回的 group_id 必须跟着 username 走，
            # 否则固定返回 -5700 会让后面轮到 -5700 的群被误判成「已在群内」
            return {'ok': True, 'group_id': -5700 - int(str(username).lstrip('@lim')), 'title': 'lim'}

        job, join_mock = await self._run_join_job(['acct_a', 'acct_b'], ['acct_a'], outcomes)

        self.assertEqual(join_mock.await_count, 4, 'A 撞一次就该整批退出，剩下 3 个群换 B')
        self.assertEqual(job['joined'] + job['skipped'], 3, job['items'])
        self.assertEqual(job['failed'], 0, job['items'])
        self.assertEqual(job['redispatches'], 3, job)
        self.assertEqual(main.account_service.cached_status('acct_a')['status'], 'ok',
                         '只是加群额度满了，账号还能发消息，不能判死')

    async def test_gives_up_after_trying_enough_accounts(self):
        """换到上限还没加进去就判失败，别拿更多账号去烧加群额度。"""
        from app.services import join_service

        self.add_groups([('@stuck', -5603)])
        for name in ('acct_a', 'acct_b', 'acct_c'):
            self.mark_ready(name)

        async def outcomes(account, username, known_joined_ids=None):
            return {'ok': False, 'error': 'join_public_failed: unknown'}

        accounts = ['acct_a', 'acct_b', 'acct_c']
        job, join_mock = await self._run_join_job(accounts, ['acct_a'], outcomes)

        self.assertEqual(join_mock.await_count, join_service.JOIN_MAX_ACCOUNTS_PER_GROUP, job['items'])
        self.assertEqual(job['failed'], 1, job['items'])
        self.assertIn('换过', job['items'][0]['error'], job['items'][0])
        self.assertEqual(job['pending'], 0, job)

    async def test_approval_required_group_is_marked_and_skipped(self):
        """要审核才能进的群：申请一次就落库标记，之后加群不再碰它。"""
        self.add_groups([('@need_approval', -5801)])
        self.mark_ready('acct_a')
        self.mark_ready('acct_b')

        def outcomes(account, username, known_joined_ids=None):
            return {'ok': False,
                    'error': 'join_public_failed: You have successfully requested to join this chat or channel'}

        job, join_mock = await self._run_join_job(['acct_a', 'acct_b'], ['acct_a'], outcomes)
        self.assertEqual(job['requested'], 1, job['items'])
        # 只申请一次：不换号、不重试
        self.assertEqual(join_mock.await_count, 1, job['items'])

        db = self.SessionLocal()
        try:
            row = db.query(self.TargetGroup).filter(self.TargetGroup.group_id == -5801).first()
            self.assertEqual(row.status, 'approval_required', row.status)
            self.assertIn('审核', row.last_error or '', row.last_error)
        finally:
            db.close()

        # 再点一次批量加群：这个群不该再进候选
        response = await self.client.post('/api/groups/pool/join', headers=self.headers,
                                          json={'accounts': ['acct_a']})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()['detail'], 'all_groups_covered', response.text)

    async def test_username_that_does_not_exist_is_not_retried(self):
        """「No user has xxx as username」= 群不存在，换号也是同一句，别白烧额度。"""
        self.add_groups([('@ghost', -5802)])
        self.mark_ready('acct_a')
        self.mark_ready('acct_b')

        async def outcomes(account, username, known_joined_ids=None):
            return {'ok': False, 'error': 'join_public_failed: No user has "ghost" as username'}

        job, join_mock = await self._run_join_job(['acct_a', 'acct_b'], ['acct_a'], outcomes)

        self.assertEqual(join_mock.await_count, 1, '群不存在就别换号了')
        self.assertEqual(job['failed'], 1, job['items'])
        self.assertIn('群不可用', job['items'][0]['error'], job['items'][0])

    def test_assignment_never_repeats_a_group(self):
        from app.services import join_service

        class Row:
            def __init__(self, gid):
                self.id, self.group_id, self.username, self.title = gid, -gid, f'@g{gid}', None

        groups = [Row(i) for i in range(1, 11)]
        assignment = join_service.build_assignment(groups, ['a', 'b', 'c'])
        assigned = [row.id for rows in assignment.values() for row in rows]
        self.assertEqual(sorted(assigned), list(range(1, 11)))
        self.assertEqual(len(assigned), len(set(assigned)))

    async def test_join_timestamp_uses_app_timezone(self):
        """last_run_at 要能被 _seconds_since_task_timestamp 正确解读。

        写 UTC、按应用时区读会差 8 小时，导致「刚加进去的群」保护期永远失效。
        """
        self.add_groups([('@ts', -5701)])
        self.mark_ready('acct_a')

        async def outcomes(account, username, known_joined_ids=None):
            return {'ok': True, 'group_id': -5701, 'title': 'ts'}

        await self._run_join_job(['acct_a'], ['acct_a'], outcomes)
        db = self.SessionLocal()
        try:
            row = db.query(self.TargetGroup).filter_by(group_id=-5701).one()
            self.assertIsNotNone(row.last_run_at)
            self.assertLess(main._seconds_since_task_timestamp(row.last_run_at), 60)
            # 加群日志的时间也得是应用时区，否则比群发日志整整差 8 小时
            log_row = (db.query(self.JoinLog).filter_by(group_id=-5701)
                       .order_by(self.JoinLog.id.desc()).first())
            self.assertIsNotNone(log_row)
            self.assertLess(main._seconds_since_task_timestamp(log_row.created_at), 60)
        finally:
            db.close()

    async def test_job_runs_accounts_in_parallel(self):
        """并发是按账号分的：多个账号同时推进，账号自己内部仍串行。"""
        from app.services import join_service
        from unittest.mock import AsyncMock

        self.add_groups([(f'@p{i}', -5300 - i) for i in range(6)])
        db = self.SessionLocal()
        try:
            groups = db.query(self.TargetGroup).order_by(self.TargetGroup.id.asc()).all()
        finally:
            db.close()

        assignment = join_service.build_assignment(groups, ['acct_a', 'acct_b'])
        self.assertEqual([len(v) for v in assignment.values()], [3, 3], assignment)

        concurrency = {'now': 0, 'peak': 0}

        async def fake_join(account, ref, known_joined_ids=None):
            concurrency['now'] += 1
            concurrency['peak'] = max(concurrency['peak'], concurrency['now'])
            await asyncio.sleep(0.01)
            concurrency['now'] -= 1
            return {'ok': True, 'group_id': None}

        with patch('app.services.join_service.multi_manager.get_joined_groups',
                   new=AsyncMock(return_value=[])), \
             patch('app.services.join_service.multi_manager.join_group', new=fake_join):
            job = join_service._create_job(assignment, 0, 2)
            await join_service._run_job(job)

        self.assertEqual(job['joined'], 6, job)
        self.assertEqual(job['failed'], 0, job)
        self.assertGreater(concurrency['peak'], 1, '两个账号应该同时在跑，而不是一个跑完再下一个')

    async def test_job_skips_groups_already_joined(self):
        """账号已在群里的目标记「已跳过」，不会再去加一次。"""
        from app.services import join_service
        from unittest.mock import AsyncMock

        self.add_groups([('@dup', -5201), ('@fresh', -5202)])
        db = self.SessionLocal()
        try:
            groups = db.query(self.TargetGroup).order_by(self.TargetGroup.id.asc()).all()
        finally:
            db.close()

        assignment = join_service.build_assignment(groups, ['acct_a'])
        join_mock = AsyncMock(return_value={'ok': True, 'group_id': -5202, 'title': 'fresh'})
        with patch('app.services.join_service.multi_manager.get_joined_groups',
                   new=AsyncMock(return_value=[{'id': -5201, 'raw_id': 5201, 'username': 'dup'}])), \
             patch('app.services.join_service.multi_manager.join_group', new=join_mock):
            job = join_service._create_job(assignment, 0, 1)
            await join_service._run_job(job)

        # 加群顺序已经随机化，这里只校验结果集合，不依赖先后
        self.assertEqual(sorted(item['status'] for item in job['items']), ['joined', 'skipped'], job['items'])
        self.assertEqual((job['joined'], job['skipped'], job['failed']), (1, 1, 0), job)
        self.assertEqual(join_mock.await_count, 1, '已在群里的目标不该再调一次加群')
        self.assertEqual(join_mock.await_args.args[1], '@fresh')

        # 真的加进去的群才写「最后执行」，账号的群组缓存要作废
        db = self.SessionLocal()
        try:
            rows = {row.username: row for row in db.query(self.TargetGroup).all()}
            self.assertIsNotNone(rows['@fresh'].last_run_at)
            self.assertIsNone(rows['@dup'].last_run_at)
        finally:
            db.close()

    async def test_account_hands_back_remaining_groups_on_flood_wait(self):
        """撞上限流要把剩下的群整批交还调度层去冷却，不能占着 worker 干等几百秒。"""
        from app.services import join_service
        from unittest.mock import AsyncMock

        self.add_groups([('@f1', -5401), ('@f2', -5402)])
        db = self.SessionLocal()
        try:
            groups = db.query(self.TargetGroup).order_by(self.TargetGroup.id.asc()).all()
        finally:
            db.close()

        assignment = join_service.build_assignment(groups, ['acct_a'])
        job = join_service._create_job(assignment, 0, 1)
        flood = AsyncMock(return_value={
            'ok': False,
            'error': 'A wait of 600 seconds is required (caused by JoinChannelRequest)',
        })
        snapshots = {}
        with patch('app.services.join_service.multi_manager.get_joined_groups',
                   new=AsyncMock(return_value=[])), \
             patch('app.services.join_service.multi_manager.join_group', new=flood):
            remaining, resume_in = await join_service._run_account(
                'acct_a', job['items'], job, 0.0, snapshots, [])

        self.assertEqual(len(remaining), 2, remaining)
        self.assertEqual(resume_in, 602.0, '要等够 Telegram 要求的秒数再回来')
        self.assertEqual(job['flood_waits'], 1, job)
        self.assertEqual(job['done'], 0, '被限流的不能算已完成')
        self.assertEqual(job['items'][0]['resumes'], 1, job['items'][0])

        # 同一个目标被推迟到上限还解不开就判失败，否则任务永远跑不完
        with patch('app.services.join_service.multi_manager.get_joined_groups',
                   new=AsyncMock(return_value=[])), \
             patch('app.services.join_service.multi_manager.join_group', new=flood):
            for _ in range(join_service.FLOOD_MAX_RESUMES):
                remaining, _ = await join_service._run_account(
                    'acct_a', job['items'], job, 0.0, snapshots, [])

        self.assertEqual(job['items'][0]['status'], 'failed', job['items'][0])
        self.assertIn('限流', job['items'][0]['error'])


class PresetTests(unittest.IsolatedAsyncioTestCase):
    """编号任务：保存就是拿一个编号，同号覆盖改内容。"""

    async def asyncSetUp(self):
        from app.database import SessionLocal
        from app.models import SendPreset, TargetGroup, Task
        self.SessionLocal = SessionLocal
        self.SendPreset = SendPreset
        self.TargetGroup = TargetGroup
        self.Task = Task
        db = SessionLocal()
        try:
            for table in (SendPreset, TargetGroup, Task):
                db.query(table).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
        self.headers = {'X-Admin-Token': 'test-only'}
        main._LAST_TS.clear()
        main._REQ_IDS.clear()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')

    async def asyncTearDown(self):
        db = self.SessionLocal()
        try:
            db.query(self.SendPreset).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
        await self.client.aclose()

    async def save_preset(self, **overrides):
        body = {'message': 'hello', 'parse_mode': 'plain', 'delay_ms': 3000,
                'rounds': 1, 'round_interval_s': 0, 'disable_web_page_preview': True}
        body.update(overrides)
        return await self.client.post('/api/send-presets/save', headers=self.headers, json=body)

    async def test_preset_is_numbered_and_can_be_overwritten(self):
        """保存就是拿一个编号；同号覆盖改内容，空正文不许保存。"""
        first = (await self.save_preset()).json()['preset']
        self.assertEqual(first['no'], 1, first)
        # 同群同文案冷却默认 180 秒，不做任何设置也生效
        self.assertEqual(first['group_msg_cooldown_s'], 180, first)
        second = (await self.save_preset(message='第二轮')).json()['preset']
        self.assertEqual(second['no'], 2, second)
        self.assertEqual([i['no'] for i in (await self.client.get('/api/send-presets', headers=self.headers)).json()['items']],
                         [1, 2])

        updated = (await self.save_preset(no=1, message='改过的', group_msg_cooldown_s=300)).json()['preset']
        self.assertEqual((updated['no'], updated['message']), (1, '改过的'), updated)
        self.assertEqual(updated['group_msg_cooldown_s'], 300, updated)

        self.assertEqual((await self.save_preset(no=1, message='   ')).status_code, 400)
        self.assertEqual((await self.save_preset(no=99, message='x')).status_code, 404)
        self.assertEqual((await self.client.post('/api/send-presets/delete', headers=self.headers,
                                                json={'no': 2})).json()['deleted'], 1)
        self.assertEqual([i['no'] for i in (await self.client.get('/api/send-presets', headers=self.headers)).json()['items']],
                         [1])


class GroupMessageCooldownTests(unittest.IsolatedAsyncioTestCase):
    """群发冷却：只看「谁在哪个群发过」，外加换号后的同群最小间隔。"""

    async def asyncSetUp(self):
        from app.database import SessionLocal
        from app.models import SendLog
        self.SessionLocal = SessionLocal
        self.SendLog = SendLog
        db = SessionLocal()
        try:
            db.query(SendLog).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def add_log(self, account, gid, preview, seconds_ago=0, status='success'):
        from datetime import timedelta
        db = self.SessionLocal()
        try:
            db.add(self.SendLog(account_name=account, group_id=gid, group_title='t',
                                message_preview=preview, status=status,
                                created_at=main.CONFIG.now() - timedelta(seconds=seconds_ago)))
            db.commit()
        finally:
            db.close()

    def test_first_send_is_never_delayed(self):
        self.assertEqual(main._group_msg_cooldown_remaining(-9001, 'acct_a', 180, 0), 0.0)

    def test_recent_send_holds_only_the_same_account(self):
        self.add_log('acct_a', -9002, 'hello')
        remaining = main._group_msg_cooldown_remaining(-9002, 'acct_a', 180, 0)
        self.assertGreater(remaining, 170, remaining)
        self.assertLessEqual(remaining, 180, remaining)
        # 换个号不再被「别的号发过」拖住：每群两三个覆盖成员才有意义
        self.assertEqual(main._group_msg_cooldown_remaining(-9002, 'acct_b', 180, 0), 0.0)

    def test_cross_account_min_gap_blocks_the_next_account(self):
        """换号也别紧挨着连发：同一个群仍要有 10 分钟的最小平隔。"""
        self.add_log('acct_other', -9003, 'hello')
        self.assertGreater(main._group_msg_cooldown_remaining(-9003, 'acct_a', 180, 600), 590)

    def test_other_group_is_free(self):
        self.add_log('acct_a', -9004, 'hello')
        self.assertEqual(main._group_msg_cooldown_remaining(-9005, 'acct_a', 180, 0), 0.0)

    def test_expired_cooldown_does_not_block(self):
        self.add_log('acct_a', -9006, 'hello', seconds_ago=200)
        self.assertEqual(main._group_msg_cooldown_remaining(-9006, 'acct_a', 180, 0), 0.0)

    def test_zero_turns_the_limit_off(self):
        self.add_log('acct_a', -9007, 'hello')
        self.assertEqual(main._group_msg_cooldown_remaining(-9007, 'acct_a', 0, 0), 0.0)

    def test_failed_send_does_not_hold_the_cooldown(self):
        """被禁言/失败的记录不算「发进去了」，下一个账号可以立刻补发。"""
        self.add_log('acct_a', -9008, 'hello', status='failed')
        self.assertEqual(main._group_msg_cooldown_remaining(-9008, 'acct_a', 180, 0), 0.0)

    def test_bulk_map_returns_only_cooling_groups(self):
        """每轮开始前整批过滤：只有真在冷却的群会被剔掉。"""
        self.add_log('acct_a', -9101, 'hello')
        self.add_log('acct_a', -9102, 'hello', seconds_ago=200)
        self.add_log('acct_b', -9103, 'hello')
        cooling = main._group_msg_cooldown_map([-9101, -9102, -9103, -9104], 'acct_a', 180, 0)
        self.assertEqual(list(cooling), [-9101], cooling)
        self.assertGreater(cooling[-9101], 170)


class AdaptiveCooldownTests(unittest.TestCase):
    """按群池规模反推冷却：池子越大同一群隔得越久，但有下限和预设上限。"""

    def test_pool_size_drives_the_interval(self):
        with patch.object(main, '_joined_group_total', return_value=218):
            with patch.object(main, 'SEND_TARGET_PER_HOUR', 240), \
                    patch.object(main, 'GROUP_MIN_GAP_S', 1800):
                # 218 个群 / 240 条每小时 ≈ 55 分钟
                self.assertEqual(main._effective_group_cooldown_s(21600), 3270)

    def test_configured_cooldown_is_the_ceiling(self):
        with patch.object(main, '_joined_group_total', return_value=10000):
            with patch.object(main, 'SEND_TARGET_PER_HOUR', 240), \
                    patch.object(main, 'GROUP_MIN_GAP_S', 1800):
                self.assertEqual(main._effective_group_cooldown_s(21600), 21600)

    def test_pool_too_small_falls_back_to_the_min_gap(self):
        with patch.object(main, '_joined_group_total', return_value=100):
            with patch.object(main, 'SEND_TARGET_PER_HOUR', 240), \
                    patch.object(main, 'GROUP_MIN_GAP_S', 1800):
                self.assertEqual(main._effective_group_cooldown_s(21600), 1800)

    def test_target_off_keeps_the_preset_value(self):
        with patch.object(main, 'SEND_TARGET_PER_HOUR', 0):
            self.assertEqual(main._effective_group_cooldown_s(21600), 21600)


class GroupGradingTests(unittest.TestCase):
    """群分级一次查完：失败多的群判黑，正常的白，没记录的先算灰。"""

    def setUp(self):
        from app.database import SessionLocal
        from app.models import SendLog
        self.SessionLocal = SessionLocal
        self.SendLog = SendLog
        self.db = SessionLocal()
        try:
            self.db.query(SendLog).delete(synchronize_session=False)
            for _ in range(3):
                self.db.add(SendLog(account_name='acct_g', group_id=-9201, status='failed',
                                    message_preview='x'))
            self.db.add(SendLog(account_name='acct_g', group_id=-9202, status='success',
                                message_preview='x'))
            self.db.commit()
        finally:
            pass

    def tearDown(self):
        self.db.close()

    def test_black_white_and_grey(self):
        grades = main.classify_groups(self.db, 'acct_g', [-9201, -9202, -9203])
        self.assertEqual(grades, {-9201: 'BLACK', -9202: 'WHITE', -9203: 'GREY'}, grades)


class MuteErrorTests(unittest.TestCase):
    """被禁言是「账号 × 群」的问题：能认出来，才谈得上换号补发。"""

    def test_mute_errors_are_detected(self):
        self.assertTrue(main.is_mute_error('UserBannedInChannelError: You are banned from sending messages'))
        self.assertTrue(main.is_mute_error('ChatWriteForbiddenError: You cannot write in this chat'))

    def test_other_errors_are_not_mute(self):
        self.assertFalse(main.is_mute_error('A wait of 2956 seconds is required'))
        self.assertFalse(main.is_mute_error('Could not find the input entity'))
        self.assertFalse(main.is_mute_error(None))

    def test_member_lost_errors_are_detected(self):
        self.assertTrue(main.is_member_lost_error(
            'The channel specified is private and you lack permission to access it.'))
        self.assertFalse(main.is_member_lost_error('user_banned_in_channel'))


class ProbeSendPermissionTests(unittest.IsolatedAsyncioTestCase):
    """发送前先查权限：被禁言/已不在群的群直接剔掉，不再靠发一条失败一条去发现。"""

    async def test_probe_splits_usable_muted_and_gone(self):
        states = {-9001: 'ok', -9002: 'blocked', -9003: 'not_member', -9004: 'unknown',
                  -9005: 'group_readonly'}

        async def fake(account, gid):
            return states[gid]

        with patch.object(main.multi_manager, 'get_send_permission', side_effect=fake):
            usable, muted, readonly, gone = await main._probe_send_permissions('acct', list(states))
        self.assertEqual(sorted(usable), [-9004, -9001])
        self.assertEqual(muted, [-9002])
        self.assertEqual(readonly, [-9005])
        self.assertEqual(gone, [-9003])

    def test_account_send_block_expires(self):
        main._ACCOUNT_SEND_BLOCK.clear()
        main._mark_account_send_blocked('acct_block', 'spam ban')
        self.assertTrue(main._account_send_blocked('acct_block'))
        main._ACCOUNT_SEND_BLOCK['acct_block'] = 0.0
        self.assertFalse(main._account_send_blocked('acct_block'))
        self.assertFalse(main._account_send_blocked('acct_fine'))

    def test_forget_membership_drops_only_that_account(self):
        from app.database import SessionLocal
        from app.models import TargetGroup
        db = SessionLocal()
        try:
            db.add(TargetGroup(group_id=-9009, username='probe_group', title='probe',
                               joined_by_json='["acct_a","acct_b"]'))
            db.commit()
            main._forget_group_membership(db, 'acct_a', -9009)
            db.commit()
            row = db.query(TargetGroup).filter(TargetGroup.group_id == -9009).first()
            self.assertEqual(row.joined_by_json, '["acct_b"]')
        finally:
            db.query(TargetGroup).filter(TargetGroup.group_id == -9009).delete(synchronize_session=False)
            db.commit()
            db.close()


class GroupRestrictionTests(unittest.IsolatedAsyncioTestCase):
    """群自己的限制（不让转发 / 不让发内联 / 话题关闭 / 需付费）要能认出来并拉黑。"""

    def test_chat_blocked_forward_is_detectable(self):
        """认出「群不让转发」才会退回发链接文本，而不是把整条消息判死。"""
        from app.telegram_client import _chat_blocks_forwards
        self.assertTrue(_chat_blocks_forwards(
            'forward_send_failed: You cannot send photos results in this chat (caused by ForwardMessagesRequest)'))
        self.assertTrue(_chat_blocks_forwards(
            'forward_send_failed: You cannot send inline results in this chat (caused by ForwardMessagesRequest)'))
        self.assertTrue(_chat_blocks_forwards('rpc_error: CHAT_FORWARDS_RESTRICTED'))
        # 账号被封、群不可达这类，退回发文本也救不了，不该白试一次
        self.assertFalse(_chat_blocks_forwards('user_banned_in_channel'))
        self.assertFalse(_chat_blocks_forwards('chat_write_forbidden'))
        self.assertFalse(_chat_blocks_forwards('Could not find the input entity for PeerChannel'))

    def test_chat_restrictions_are_permanent(self):
        from app.services.group_service import should_add_to_blist
        for err in ('forward_send_failed: You cannot send photos results in this chat',
                    'forward_send_failed: You cannot send inline results in this chat (caused by ForwardMessagesRequest)',
                    'forward_send_failed: RPCError 400: TOPIC_CLOSED',
                    'forward_send_failed: RPCError 403: ALLOW_PAYMENT_REQUIRED_35000',
                    'chat_write_forbidden'):
            self.assertTrue(should_add_to_blist(err), err)

    def test_chat_properties_go_global_but_group_bans_do_not(self):
        """群自身属性（不让转发/不让发内联/禁言）一个账号撞上就整群下架，其余账号不必再各试一遍。

        「你被本群禁言」不算群属性：那是 (账号,群) 级别的，一个账号撞上就下架整群会把
        绝大多数目标误杀（8003 实测 507 个群被下架 494 个，全库只剩 2 个可发组合）。
        它走 needs_ban_vote 投票，够票数才下架。
        """
        from app.services.group_service import should_add_to_global_blist, needs_ban_vote
        for err in ('You cannot send photos results in this chat', 'TOPIC_CLOSED',
                    'ALLOW_PAYMENT_REQUIRED_35000', 'chat_write_forbidden'):
            self.assertTrue(should_add_to_global_blist(err), err)

        self.assertFalse(should_add_to_global_blist('user_banned_in_channel'))
        self.assertTrue(needs_ban_vote('user_banned_in_channel'))
        self.assertFalse(needs_ban_vote('chat_write_forbidden'))
        # 账号自己的网络/风控问题不该连累整个群
        self.assertFalse(should_add_to_global_blist('floodwait 60 seconds is required'))
        self.assertFalse(should_add_to_global_blist('TimeoutError: request timed out'))


class JoinBackupSlotTests(unittest.TestCase):
    """每个群要配 2 个成员账号当备胎，并且不会把群再分给已经在里面的账号。"""

    def _group(self, username, members, status='available'):
        import json
        import types
        return types.SimpleNamespace(status=status, username=username,
                                     joined_by_json=json.dumps(list(members)))

    def test_groups_with_one_member_still_need_a_backup(self):
        from app.services.join_service import uncovered_groups
        groups = [
            self._group('@one', ['acct_a']),
            self._group('@two', ['acct_a', 'acct_b']),
            self._group('@none', []),
            self._group('@appr', [], status='approval_required'),
        ]
        got = [g.username for g in uncovered_groups(groups, ['acct_a', 'acct_b'])]
        self.assertEqual(got, ['@one', '@none'], got)

    def test_assignment_never_picks_an_existing_member(self):
        from app.services.join_service import build_assignment
        got = build_assignment([self._group('@one', ['acct_a'])], ['acct_a', 'acct_b'])
        self.assertEqual({k: [x.username for x in v] for k, v in got.items()},
                         {'acct_b': ['@one']}, got)


class LiveModeTests(unittest.TestCase):
    """边加群边群发：动态任务从群池实时取「已加入 + 未拉黑」的群。"""

    def test_pool_group_ids_only_returns_sendable_joined_groups(self):
        import json
        import types
        from unittest.mock import Mock, patch
        import main

        groups = [
            types.SimpleNamespace(group_id=1, status='available',
                                  joined_by_json=json.dumps(['acct'])),
            types.SimpleNamespace(group_id=2, status='available',
                                  joined_by_json=json.dumps(['other'])),
            types.SimpleNamespace(group_id=3, status='available',
                                  joined_by_json=json.dumps(['acct'])),
            types.SimpleNamespace(group_id=4, status='unreachable',
                                  joined_by_json=json.dumps(['acct'])),
            types.SimpleNamespace(group_id=5, status='approval_required',
                                  joined_by_json=json.dumps(['acct'])),
            types.SimpleNamespace(group_id=None, status='available',
                                  joined_by_json=json.dumps(['acct'])),
        ]
        db = Mock()
        db.query.return_value.all.return_value = groups
        with patch.object(main, 'get_banned_group_ids', return_value=[3]):
            got = main._account_pool_group_ids(db, 'acct')
        # 1 可发；2 不是该账号加入的；3 被拉黑；4/5 群状态不可发；None 未解析
        self.assertEqual(got, [1], got)

    def test_create_live_task_marks_it_dynamic(self):
        from unittest.mock import Mock
        import main

        db = Mock()
        preset = {
            'message': 'hi', 'parse_mode': 'plain', 'disable_web_page_preview': True,
            'delay_ms': 60000, 'rounds': 100, 'round_interval_s': 600,
            'group_msg_cooldown_s': 180,
        }
        task_id = main._create_live_task(db, 'acct', [11, 12], preset)
        created = db.add.call_args_list[0].args[0]
        self.assertEqual(created.id, task_id)
        self.assertEqual(created.dynamic_groups, 1)
        self.assertEqual(created.total, 2)
        self.assertEqual(created.rounds, 100)
        self.assertEqual(created.round_interval_s, 600)
        # 冷却按预设走，不再被偷偷改成「一天一次」
        self.assertEqual(created.group_msg_cooldown_s, 180)

    def test_join_flush_skips_dynamic_tasks(self):
        """加群回写不要碰动态任务：它每轮自己从群池重算。"""
        import json
        from app.database import SessionLocal
        from app.models import Task

        db = SessionLocal()
        try:
            db.add(Task(id='t-dyn-test', status='running', total=0, success=0, failed=0,
                        account_name='acct_dyn', message='x', group_ids_json='[]',
                        dynamic_groups=1))
            db.commit()
            main.join_service._append_group_to_running_tasks(db, 'acct_dyn', 12345)
            db.commit()
            row = db.query(Task).filter(Task.id == 't-dyn-test').one()
            self.assertEqual(json.loads(row.group_ids_json), [])
        finally:
            db.query(Task).filter(Task.id == 't-dyn-test').delete()
            db.commit()
            db.close()

    def test_coverage_assignment_only_fills_gaps(self):
        import json
        import types
        from unittest.mock import Mock, patch
        import main

        groups = [
            types.SimpleNamespace(group_id=1, status='available', username='@one',
                                  joined_by_json=json.dumps(['a'])),
            types.SimpleNamespace(group_id=2, status='available', username='@two',
                                  joined_by_json=json.dumps([])),
            types.SimpleNamespace(group_id=3, status='available', username='@three',
                                  joined_by_json=json.dumps(['a', 'b'])),
        ]
        with patch.object(main, '_pool_join_groups', return_value=groups), \
                patch.object(main, 'get_banned_group_ids', return_value=[]):
            got = main._coverage_join_assignment(Mock(), ['a', 'b'], 2)
        assigned = {acc: sorted(g.group_id for g in items) for acc, items in got.items()}
        # 群 1 只缺 1 个 -> b；群 2 缺 2 个 -> a、b；群 3 已经够 2 个 -> 不动
        self.assertEqual(assigned, {'a': [2], 'b': [1, 2]}, assigned)

    def test_empty_groups_are_joined_first(self):
        """加群优先级：一个账号都没有的群排在「已经有成员但覆盖不足」的群前面。"""
        import json
        import types
        from unittest.mock import Mock, patch
        import main

        groups = [
            types.SimpleNamespace(group_id=1, status='available', username='@half',
                                  joined_by_json=json.dumps(['a'])),
            types.SimpleNamespace(group_id=2, status='available', username='@empty',
                                  joined_by_json=json.dumps([])),
        ]
        with patch.object(main, '_pool_join_groups', return_value=groups), \
                patch.object(main, 'get_banned_group_ids', return_value=[]):
            got = main._coverage_join_assignment(Mock(), ['a', 'b'], 2)
        # b 两个群都要加：空群（2）必须排在已有成员的群（1）前面
        self.assertEqual([g.group_id for g in got['b']], [2, 1], got)

    def test_stopped_task_is_rebuilt_instead_of_resumed(self):
        """被平台限制发言停掉的任务必须重建，不能拿旧任务重拉（stop_requested 还是 1）。"""
        import main

        for status in ('done', 'completed', 'stopped', 'error', None):
            self.assertTrue(main._live_task_needs_rebuild(status), status)
        for status in ('running', 'scheduled', 'paused'):
            self.assertFalse(main._live_task_needs_rebuild(status), status)


class MembershipRefreshTests(unittest.IsolatedAsyncioTestCase):
    """刷新成员关系时，刚加成功的群不能被「列表里还没有」冲掉。"""

    async def asyncSetUp(self):
        from datetime import timedelta
        from app.database import SessionLocal
        from app.models import TargetGroup
        self.SessionLocal = SessionLocal
        self.TargetGroup = TargetGroup
        db = SessionLocal()
        try:
            db.query(TargetGroup).delete(synchronize_session=False)
            db.add(TargetGroup(username='@fresh', kind='public', group_id=-9301,
                               status='available', joined_by_json='["acct_m"]',
                               last_run_at=main.CONFIG.now() - timedelta(seconds=60)))
            db.add(TargetGroup(username='@old', kind='public', group_id=-9302,
                               status='available', joined_by_json='["acct_m"]',
                               last_run_at=main.CONFIG.now() - timedelta(hours=2)))
            db.add(TargetGroup(username='@race', kind='public', group_id=-9303,
                               status='available', joined_by_json='[]'))
            db.commit()
        finally:
            db.close()

    async def asyncTearDown(self):
        db = self.SessionLocal()
        try:
            db.query(self.TargetGroup).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def _members(self, username):
        import json
        db = self.SessionLocal()
        try:
            row = db.query(self.TargetGroup).filter_by(username=username).one()
            return json.loads(row.joined_by_json or '[]')
        finally:
            db.close()

    async def test_fresh_join_survives_stale_dialog_list(self):
        from unittest.mock import AsyncMock
        # 拉到的列表里只有别的群：新加的那个群还没进列表
        with patch.object(main.multi_manager, 'get_joined_groups',
                          new=AsyncMock(return_value=[{'id': -9999, 'raw_id': -9999, 'username': 'other'}])):
            await main._refresh_account_membership('acct_m')
        self.assertEqual(self._members('@fresh'), ['acct_m'])
        # 两小时前加的群确实不在列表里，说明人已经退了，要摘掉
        self.assertEqual(self._members('@old'), [])

    async def test_membership_added_during_fetch_is_kept(self):
        """拉列表期间刚加成功的群，这次列表里当然没有，但不能被当成「人退了」。"""
        async def fetch(account, only_groups=False):
            db = self.SessionLocal()
            try:
                row = db.query(self.TargetGroup).filter_by(username='@race').one()
                row.joined_by_json = '["acct_m"]'
                row.last_run_at = main.CONFIG.now()
                db.commit()
            finally:
                db.close()
            return [{'id': -9999, 'raw_id': -9999, 'username': 'other'}]

        with patch.object(main.multi_manager, 'get_joined_groups', new=fetch):
            await main._refresh_account_membership('acct_m')
        self.assertEqual(self._members('@race'), ['acct_m'])


class GroupBanVoteTests(unittest.TestCase):
    """「这个群禁言了某个账号」要够票数才整群下架，一票只拉黑当事账号。"""

    def setUp(self):
        from app.database import SessionLocal
        from app.models import SystemKV
        self.SessionLocal = SessionLocal
        self.SystemKV = SystemKV
        self._clear()

    def tearDown(self):
        self._clear()

    def _clear(self):
        db = self.SessionLocal()
        try:
            db.query(self.SystemKV).filter(
                (self.SystemKV.k.like('ban_vote%'))
                | (self.SystemKV.k.like('banned_groups%'))).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def _votes(self, gid):
        from app.services.group_service import get_banned_group_ids
        db = self.SessionLocal()
        try:
            return get_banned_group_ids(db, 'acct_a'), get_banned_group_ids(db)
        finally:
            db.close()

    def test_same_account_votes_only_once(self):
        from app.services.group_service import register_ban_vote
        db = self.SessionLocal()
        try:
            self.assertEqual(register_ban_vote(db, -1001, 'acct_a'), 1)
            self.assertEqual(register_ban_vote(db, -1001, 'acct_a'), 1)
            self.assertEqual(register_ban_vote(db, -1002, 'acct_a'), 1)
        finally:
            db.close()

    def test_votes_are_counted_per_group(self):
        from app.services.group_service import register_ban_vote
        db = self.SessionLocal()
        try:
            for i, name in enumerate(('acct_a', 'acct_b', 'acct_c')):
                self.assertEqual(register_ban_vote(db, -1001, name), i + 1)
            self.assertEqual(register_ban_vote(db, -2002, 'acct_a'), 1)
        finally:
            db.close()

    def test_two_votes_do_not_take_the_group_global(self):
        from app.services.group_service import register_ban_vote, GLOBAL_BAN_VOTES_REQUIRED
        self.assertGreaterEqual(GLOBAL_BAN_VOTES_REQUIRED, 2)
        db = self.SessionLocal()
        try:
            register_ban_vote(db, -1001, 'acct_a')
            votes = register_ban_vote(db, -1001, 'acct_b')
        finally:
            db.close()
        self.assertEqual(votes, 2)
        self.assertLess(votes, GLOBAL_BAN_VOTES_REQUIRED)
        _per, glob = self._votes(-1001)
        self.assertEqual(glob, [])

    def test_reaching_the_threshold_is_reported(self):
        """够票数时返回值要让调用方去写全局名单——阈值 3 就是第 3 个账号。"""
        from app.services.group_service import register_ban_vote, GLOBAL_BAN_VOTES_REQUIRED
        db = self.SessionLocal()
        try:
            votes = 0
            for name in ('acct_a', 'acct_b', 'acct_c', 'acct_d'):
                votes = register_ban_vote(db, -1001, name)
                if votes >= GLOBAL_BAN_VOTES_REQUIRED:
                    break
        finally:
            db.close()
        self.assertGreaterEqual(votes, GLOBAL_BAN_VOTES_REQUIRED)


class GroupBanMapTests(unittest.TestCase):
    """一次读出的黑名单字典要同时含全局和每个账号自己的。"""

    def setUp(self):
        from app.database import SessionLocal
        from app.models import SystemKV
        self.SessionLocal = SessionLocal
        self.SystemKV = SystemKV
        db = SessionLocal()
        try:
            db.query(self.SystemKV).filter(self.SystemKV.k.like('banned_groups%')).delete(synchronize_session=False)
            db.add(self.SystemKV(k='banned_groups:global', v='[-1]'))
            db.add(self.SystemKV(k='banned_groups:acct_a', v='[-2]'))
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        db = self.SessionLocal()
        try:
            db.query(self.SystemKV).filter(self.SystemKV.k.like('banned_groups%')).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def test_global_and_per_account_are_merged(self):
        from app.services.group_service import get_banned_map
        db = self.SessionLocal()
        try:
            got = get_banned_map(db, ['acct_a', 'acct_b'])
        finally:
            db.close()
        self.assertEqual(got['acct_a'], {-1, -2}, got)
        self.assertEqual(got['acct_b'], {-1}, got)


class CachedStatusFreshnessTests(unittest.IsolatedAsyncioTestCase):
    """两份检测缓存要取最新的，否则「重检被挡账号」的结果会被启动时的旧快照盖掉。"""

    def setUp(self):
        import time
        self.account_service = main.account_service
        self.name = 'cache_probe'
        self.keys = [(self.name, False), (self.name, True)]
        for key in self.keys:
            self.account_service._cache.pop(key, None)
        now = time.time()
        # 启动时从库里读回的旧快照：当时一个群都没加，被判「无群发言权限」
        self.account_service._cache[(self.name, False)] = (now - 600, {'status': 'cannot_send', 'valid': True})
        # 重检之后的新结果：现在能发了
        self.account_service._cache[(self.name, True)] = (now, {'status': 'ok', 'valid': True})

    def tearDown(self):
        for key in self.keys:
            self.account_service._cache.pop(key, None)

    def test_fresher_check_wins(self):
        status = self.account_service.cached_status(self.name)
        self.assertEqual(status['status'], 'ok', status)

    def test_rechecked_account_enters_the_send_pool(self):
        ready, blocked = main.classify_ready_accounts([self.name])
        self.assertEqual(ready, [self.name], blocked)

    def test_stale_snapshot_is_not_treated_as_ready(self):
        """过期快照不算可用，归类成「需重新检测」而不是「状态未知」。"""
        import time
        from app.services.plan_service import STATUS_TRUST_SECONDS

        old = time.time() - STATUS_TRUST_SECONDS - 1
        for key in self.keys:
            self.account_service._cache[key] = (old, {'status': 'ok', 'valid': True})
        ready, blocked = main.classify_ready_accounts([self.name])
        self.assertEqual(ready, [], blocked)
        self.assertEqual(blocked, {'检测结果已过期，需重新检测': 1}, blocked)


class AccountStatusPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """检测结果要活过容器重启，否则每次重建都要重跑一遍全量检测。"""

    async def test_status_survives_restart(self):
        from app.database import SessionLocal
        from app.models import SystemKV
        from app.services.account_service import STATUS_KEY_PREFIX

        service = main.account_service
        key = STATUS_KEY_PREFIX + 'acct_persist'
        db = SessionLocal()
        try:
            db.query(SystemKV).filter(SystemKV.k == key).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

        service._cache.pop(('acct_persist', False), None)
        service._persist_status('acct_persist', {'account': 'acct_persist', 'status': 'ok', 'valid': True})
        # 模拟重启：内存清空后从库里恢复
        service._cache.clear()
        self.assertGreaterEqual(service.load_persisted_statuses(), 1)
        cached = service.cached_status('acct_persist')
        self.assertEqual(cached['status'], 'ok', cached)
        self.assertTrue(cached['valid'], cached)

        db = SessionLocal()
        try:
            db.query(SystemKV).filter(SystemKV.k == key).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
        service._cache.pop(('acct_persist', False), None)


class AccountFailureClassificationTests(unittest.IsolatedAsyncioTestCase):
    """掉线 / 封号 / 连不上要分得开。8004 实测：42 个已掉线的号被塞进了「连接失败」。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = main.CONFIG.SESSION_DIR
        main.CONFIG.SESSION_DIR = self.tmp.name
        Path(self.tmp.name, 'acct_probe.session').write_bytes(b'')
        self.service = main.account_service
        self.saved_manager = self.service._manager

    async def asyncTearDown(self):
        main.account_service._manager = self.saved_manager
        main.account_service._cache.pop(('acct_probe', False), None)
        main.CONFIG.SESSION_DIR = self.previous
        self.tmp.cleanup()

    def manager_raising(self, message):
        class _Mgr:
            client = None

            async def ensure_connected(self):
                raise RuntimeError(message)

        class _Manager:
            def get(self, name):
                return _Mgr()

        return _Manager()

    async def test_dead_session_lands_in_unauthorized(self):
        self.service._manager = self.manager_raising('Telegram session not authorized')
        row = await self.service._do_check('acct_probe')
        self.assertEqual(row['status'], 'unauthorized', row)

    async def test_auth_key_error_lands_in_unauthorized(self):
        self.service._manager = self.manager_raising(
            'The key is not registered in the system (caused by GetUsersRequest)')
        row = await self.service._do_check('acct_probe')
        self.assertEqual(row['status'], 'unauthorized', row)

    async def test_network_error_stays_connect_failed(self):
        self.service._manager = self.manager_raising('Connection reset by peer')
        row = await self.service._do_check('acct_probe')
        self.assertEqual(row['status'], 'connect_failed', row)

    async def test_missing_proxy_refusal_says_why(self):
        self.service._manager = self.manager_raising(
            '账号 acct_probe 没有绑定可用代理，已按「强制一账号一代理」拒绝连接')
        row = await self.service._do_check('acct_probe')
        self.assertEqual(row['status'], 'connect_failed', row)
        self.assertIn('没有绑定可用代理', row['detail'], row)


class ProxyPolicyGuardTests(unittest.TestCase):
    """开启「强制一账号一代理」后，没有绑定代理的账号一律不许连 Telegram。

    8002/8004 的教训：100 个新号没代理、同一个出口 IP 连出去，几分钟掉 78 个。
    """

    def setUp(self):
        from app.database import SessionLocal
        from app.models import AccountProxy
        from app.services.proxy_service import set_proxy_enforce
        from app.telegram_client import AccountClientManager

        self.SessionLocal = SessionLocal
        self.AccountProxy = AccountProxy
        self.set_proxy_enforce = set_proxy_enforce
        self.client = AccountClientManager('acct_noproxy')
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        db = self.SessionLocal()
        try:
            self.set_proxy_enforce(db, False)
            db.query(self.AccountProxy).filter(
                self.AccountProxy.account_name == 'acct_noproxy').delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def bind_proxy(self):
        db = self.SessionLocal()
        try:
            db.add(self.AccountProxy(account_name='acct_noproxy', enabled=1, proxy_type='socks5',
                                     host='127.0.0.1', port=1080))
            db.commit()
        finally:
            db.close()

    def enforce(self, on):
        db = self.SessionLocal()
        try:
            self.set_proxy_enforce(db, on)
        finally:
            db.close()

    def test_refused_without_binding_when_enforced(self):
        self.enforce(True)
        with self.assertRaises(RuntimeError) as ctx:
            self.client.assert_proxy_policy()
        self.assertIn('没有绑定可用代理', str(ctx.exception))

    def test_allowed_when_binding_exists(self):
        self.enforce(True)
        self.bind_proxy()
        self.client.assert_proxy_policy()

    def test_allowed_when_switch_is_off(self):
        self.enforce(False)
        self.client.assert_proxy_policy()


class ProxyTopupTests(unittest.IsolatedAsyncioTestCase):
    """自动补绑：只补没代理的账号，已有绑定不动，且必须派生粘性会话。"""

    async def asyncSetUp(self):
        from app.database import SessionLocal
        from app.models import AccountProxy
        from app.services.proxy_service import set_default_proxy_source, upsert_proxy_binding

        self.SessionLocal = SessionLocal
        self.AccountProxy = AccountProxy
        self.set_default_proxy_source = set_default_proxy_source
        self.upsert = upsert_proxy_binding
        self._cleanup()
        db = SessionLocal()
        try:
            set_default_proxy_source(db, 'global.rotgb.711proxy.com:10000:USER143536-zone-custom:secret')
            self.upsert(db, 'acct_x', 'socks5://USER143536-zone-custom-session-acct_x:secret@global.rotgb.711proxy.com:10000')
        finally:
            db.close()

    async def asyncTearDown(self):
        self._cleanup()

    def _cleanup(self):
        from app.models import SystemKV

        db = self.SessionLocal()
        try:
            db.query(self.AccountProxy).filter(
                self.AccountProxy.account_name.in_(['acct_x', 'acct_y'])).delete(synchronize_session=False)
            db.query(SystemKV).filter(SystemKV.k == 'proxy_topup_last').delete(synchronize_session=False)
            self.set_default_proxy_source(db, '')
            db.commit()
        finally:
            db.close()

    async def test_binds_only_unbound_with_sticky_session(self):
        from app.services.proxy_service import get_proxy_binding

        accounts = ['acct_x', 'acct_y', 'acct_z']
        with patch.object(main, '_discover_session_accounts', return_value=accounts):
            summary = await main._proxy_topup_once()

        self.assertEqual(summary['candidates'], 2, summary)
        self.assertEqual(summary['assigned_total'], 2, summary)

        db = self.SessionLocal()
        try:
            kept = get_proxy_binding(db, 'acct_x')
            self.assertIn('-session-acct_x', kept.username, '已有绑定不能被改动')

            usernames = []
            for name in ('acct_y', 'acct_z'):
                bound = get_proxy_binding(db, name)
                self.assertIsNotNone(bound, '缺代理的账号应该被补上')
                # 共用一个轮换网关时必须派生粘性会话，否则出口 IP 会漂移、多账号撞同一个 IP。
                # 粘性标识会去掉非字母数字（acct_y -> accty），与 711Proxy 的账号名规则一致。
                token = name.replace('_', '')
                self.assertIn(f'-session-{token}', bound.username, bound.username)
                usernames.append(bound.username)
            self.assertEqual(len(set(usernames)), 2, '两个账号必须拿到不同的粘性标识')
        finally:
            db.close()

    async def test_nothing_to_do_when_all_bound(self):
        with patch.object(main, '_discover_session_accounts', return_value=['acct_x']):
            summary = await main._proxy_topup_once()
        self.assertEqual(summary['reason'], 'all_bound', summary)
        self.assertEqual(summary['assigned_total'], 0, summary)


class SpintaxTests(unittest.TestCase):
    """文案变体：带 {a|b} 的文案每条随机取一个分支，没标记的必须原样发。"""

    def test_plain_message_is_untouched(self):
        from app.services import spintax
        for text in ('你好，这是普通文案', '带个 { 花括号但不是变体', 'https://t.me/xxx'):
            self.assertEqual(spintax.expand(text), text, text)
            self.assertEqual(spintax.count(text), 1, text)

    def test_branch_is_replaced(self):
        from app.services import spintax
        out = {spintax.expand('{你好|您好|hi}') for _ in range(80)}
        self.assertTrue(out <= {'你好', '您好', 'hi'}, out)
        self.assertGreater(len(out), 1)

    def test_nested_branches(self):
        from app.services import spintax
        out = {spintax.expand('你{好{呀|哇}|早}') for _ in range(120)}
        self.assertTrue(out <= {'你好呀', '你好哇', '你早'}, out)

    def test_escape_keeps_literal_braces(self):
        from app.services import spintax
        self.assertEqual(spintax.expand(r'\{原样\}'), '{原样}')

    def test_count_multiplies_branches(self):
        from app.services import spintax
        self.assertEqual(spintax.count('{a|b}{c|d|e}'), 6)

    def test_validate_warns_when_there_is_nothing_to_vary(self):
        from app.services import spintax
        result = spintax.validate('没有变体标记')
        self.assertTrue(result['ok'])
        self.assertEqual(result['variants'], 1)
        self.assertIsNotNone(result['warning'])

    def test_unclosed_brace_is_reported_by_validate(self):
        from app.services import spintax
        result = spintax.validate('{没闭合')
        self.assertFalse(result['ok'])
        self.assertEqual(result['variants'], 0)

    def test_fingerprint_expands_spintax(self):
        from app.services.send_scheduler import SendScheduler
        sched = SendScheduler(None)
        # 关掉 emoji/空白干扰，单独看 spintax 有没有真的换词
        with patch.object(main.CONFIG, 'MESSAGE_FINGERPRINT_ENABLED', 1), \
             patch.object(main.CONFIG, 'SPINTAX_ENABLED', 1), \
             patch.object(main.CONFIG, 'MESSAGE_EMOJI_TOGGLE_PROB', 0.0), \
             patch.object(main.CONFIG, 'MESSAGE_WHITESPACE_JITTER_PROB', 0.0):
            out = {sched.fingerprint_message('{甲|乙}', 'plain') for _ in range(80)}
        self.assertTrue(out <= {'甲', '乙'}, out)

    def test_fingerprint_keeps_message_when_spintax_disabled(self):
        from app.services.send_scheduler import SendScheduler
        sched = SendScheduler(None)
        with patch.object(main.CONFIG, 'MESSAGE_FINGERPRINT_ENABLED', 1), \
             patch.object(main.CONFIG, 'SPINTAX_ENABLED', 0), \
             patch.object(main.CONFIG, 'MESSAGE_EMOJI_TOGGLE_PROB', 0.0), \
             patch.object(main.CONFIG, 'MESSAGE_WHITESPACE_JITTER_PROB', 0.0):
            self.assertEqual(sched.fingerprint_message('{甲|乙}', 'plain'), '{甲|乙}')


class SendSuffixTests(unittest.TestCase):
    """每条消息结尾都补一个随机符号/emoji，避免同一条文本被反复原样发出。"""

    def test_suffix_is_appended(self):
        from app.telegram_client import _SEND_SUFFIX_POOL, append_send_suffix
        out = append_send_suffix('广告文案')
        self.assertTrue(out.startswith('广告文案 '), out)
        self.assertIn(out.split(' ')[-1], _SEND_SUFFIX_POOL, out)

    def test_suffix_varies_between_sends(self):
        from app.telegram_client import append_send_suffix
        out = {append_send_suffix('同一条文案') for _ in range(80)}
        self.assertGreater(len(out), 1, out)

    def test_empty_and_disabled_are_untouched(self):
        from app.telegram_client import append_send_suffix
        self.assertEqual(append_send_suffix(''), '')
        with patch.object(main.CONFIG, 'CONTENT_FINGERPRINT_ENABLED', 0, create=True):
            self.assertEqual(append_send_suffix('原样'), '原样')


class WarmupTierTests(unittest.TestCase):
    """新号要预热毕业才准进群发；成功发出过消息的老号直接豁免。"""

    ACCOUNT = 'warmup_tier_acct'

    def setUp(self):
        from app.database import SessionLocal
        from app.models import SystemKV, SendLog
        from app.services import warmup
        self.SessionLocal, self.SystemKV, self.SendLog = SessionLocal, SystemKV, SendLog
        self.warmup = warmup
        # 整个类都在测门禁，所以显式把预热打开（测试环境默认是关的）
        self._enabled = patch.object(main.CONFIG, 'WARMUP_ENABLED', 1)
        self._enabled.start()
        self._clear()

    def tearDown(self):
        self._enabled.stop()
        self._clear()

    def _clear(self):
        db = self.SessionLocal()
        try:
            db.query(self.SystemKV).filter(
                self.SystemKV.k.like(f'{self.warmup.WARMUP_KEY_PREFIX}%')).delete(synchronize_session=False)
            db.query(self.SendLog).filter(self.SendLog.account_name == self.ACCOUNT).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
        self.warmup.invalidate_exempt_cache()

    def _seed_send(self, status='success'):
        db = self.SessionLocal()
        try:
            db.add(self.SendLog(account_name=self.ACCOUNT, group_id=-1001, status=status, message_preview='x'))
            db.commit()
        finally:
            db.close()
        self.warmup.invalidate_exempt_cache()

    def test_fresh_account_starts_at_new(self):
        self.assertEqual(self.warmup.tier_for(self.ACCOUNT), self.warmup.TIER_NEW)

    def test_new_account_is_not_allowed_to_send(self):
        allowed, reason = self.warmup.gate(self.ACCOUNT)
        self.assertFalse(allowed)
        self.assertIn('预热', reason)

    def test_account_with_send_history_is_mature(self):
        """老号豁免是这个功能的保命条款：上线当天不能把存量号全拦住。"""
        self._seed_send()
        self.assertEqual(self.warmup.tier_for(self.ACCOUNT), self.warmup.TIER_MATURE)
        allowed, _ = self.warmup.gate(self.ACCOUNT)
        self.assertTrue(allowed)

    def test_failed_send_does_not_grant_exemption(self):
        """一次都没发成功不算老号，否则新号随便跑一趟任务就绕过预热了。"""
        self._seed_send(status='failed')
        self.assertEqual(self.warmup.tier_for(self.ACCOUNT), self.warmup.TIER_NEW)

    def test_warming_account_is_blocked(self):
        self.warmup.save_state(self.ACCOUNT, {'readiness': 10, 'cycles': 1, 'started_at': 'x'})
        self.assertEqual(self.warmup.tier_for(self.ACCOUNT), self.warmup.TIER_WARMING)
        self.assertFalse(self.warmup.gate(self.ACCOUNT)[0])

    def test_readiness_over_threshold_graduates(self):
        threshold = self.warmup.ready_score()
        self.warmup.save_state(self.ACCOUNT, {'readiness': threshold, 'cycles': 6, 'started_at': 'x'})
        self.assertEqual(self.warmup.tier_for(self.ACCOUNT), self.warmup.TIER_TRUSTED)
        self.assertTrue(self.warmup.gate(self.ACCOUNT)[0])

    def test_mature_tier_does_not_reslow_existing_tasks(self):
        """成熟档不额外限速，老号仍然只听任务自己的 delay_ms。"""
        self._seed_send()
        self.assertEqual(self.warmup.interval_for(self.ACCOUNT), 0)

    def test_graduated_tier_has_a_floor_interval(self):
        threshold = self.warmup.ready_score()
        self.warmup.save_state(self.ACCOUNT, {'readiness': threshold, 'started_at': 'x'})
        self.assertGreater(self.warmup.interval_for(self.ACCOUNT), 0)

    def test_disabling_warmup_lets_everyone_through(self):
        with patch.object(main.CONFIG, 'WARMUP_ENABLED', 0):
            self.assertTrue(self.warmup.gate(self.ACCOUNT)[0])


class WarmupStateTests(unittest.TestCase):
    """预热进度要能活过重启，账号删掉时要跟着清干净。"""

    ACCOUNT = 'warmup_state_acct'

    def setUp(self):
        from app.database import SessionLocal
        from app.models import SystemKV
        from app.services import warmup
        self.SessionLocal, self.SystemKV = SessionLocal, SystemKV
        self.warmup = warmup
        self._clear()

    def tearDown(self):
        self._clear()

    def _clear(self):
        db = self.SessionLocal()
        try:
            db.query(self.SystemKV).filter(
                self.SystemKV.k.like(f'{self.warmup.WARMUP_KEY_PREFIX}%')).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def test_state_round_trip(self):
        self.warmup.save_state(self.ACCOUNT, {'readiness': 20, 'cycles': 2,
                                             'started_at': '2026-01-01T00:00:00+00:00'})
        state = self.warmup.load_state(self.ACCOUNT)
        self.assertEqual(state['readiness'], 20)
        self.assertEqual(state['cycles'], 2)

    def test_missing_state_reads_as_fresh(self):
        state = self.warmup.load_state(self.ACCOUNT)
        self.assertEqual(state['readiness'], 0)
        self.assertIsNone(state['started_at'])

    def test_forget_state_removes_the_row(self):
        self.warmup.save_state(self.ACCOUNT, {'readiness': 20})
        self.warmup.forget_state(self.ACCOUNT)
        self.assertEqual(self.warmup.load_state(self.ACCOUNT)['readiness'], 0)

    def test_snapshot_reports_tier_and_score(self):
        threshold = self.warmup.ready_score()
        self.warmup.save_state(self.ACCOUNT, {'readiness': threshold, 'started_at': 'x'})
        row = self.warmup.snapshot([self.ACCOUNT])[0]
        self.assertEqual(row['tier'], self.warmup.TIER_TRUSTED)
        self.assertEqual(row['ready_score'], threshold)

    def test_timeout_failure_still_records_a_reason(self):
        """TimeoutError 的 str() 是空串，失败原因不能记成一片空白（实测踩过）。"""
        with patch.object(self.warmup, '_read_cycle', side_effect=asyncio.TimeoutError()):
            result = asyncio.run(self.warmup.run_cycle(None, self.ACCOUNT))
        self.assertFalse(result['ok'])
        self.assertEqual(self.warmup.load_state(self.ACCOUNT)['last_error'], 'TimeoutError')


class WarmupBackfillTests(unittest.TestCase):
    """存量账号必须在功能上线时一次性豁免，否则发送能力会被清零。"""

    ACCOUNT = 'warmup_backfill_acct'

    def setUp(self):
        from app.database import SessionLocal
        from app.models import SystemKV
        from app.services import warmup
        self.SessionLocal, self.SystemKV = SessionLocal, SystemKV
        self.warmup = warmup
        self._enabled = patch.object(main.CONFIG, 'WARMUP_ENABLED', 1)
        self._enabled.start()
        self._clear()

    def tearDown(self):
        self._enabled.stop()
        self._clear()

    def _clear(self):
        db = self.SessionLocal()
        try:
            db.query(self.SystemKV).filter(
                self.SystemKV.k == f'{self.warmup.WARMUP_KEY_PREFIX}{self.ACCOUNT}').delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
        self.warmup.invalidate_exempt_cache()

    def test_backfill_marks_accounts_as_mature_without_send_history(self):
        """8003 的 send_logs 是空表，只靠发送记录判老号会得到空集——这条就是保命条款。"""
        result = self.warmup.backfill_existing([self.ACCOUNT])
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['marked'], 1)
        self.assertEqual(self.warmup.tier_for(self.ACCOUNT), self.warmup.TIER_MATURE)
        self.assertTrue(self.warmup.gate(self.ACCOUNT)[0])

    def test_backfill_is_idempotent(self):
        self.warmup.backfill_existing([self.ACCOUNT])
        self.assertEqual(self.warmup.backfill_existing([self.ACCOUNT])['marked'], 0)

    def test_backfill_does_not_touch_accounts_that_already_have_state(self):
        """已经在预热里的号不能被重新打标成成熟，否则养号白养。"""
        self.warmup.save_state(self.ACCOUNT, {'readiness': 10, 'cycles': 1, 'started_at': 'x'})
        self.assertEqual(self.warmup.backfill_existing([self.ACCOUNT])['marked'], 0)
        self.assertEqual(self.warmup.tier_for(self.ACCOUNT), self.warmup.TIER_WARMING)

    def test_backfill_does_nothing_when_warmup_disabled(self):
        with patch.object(main.CONFIG, 'WARMUP_ENABLED', 0):
            result = self.warmup.backfill_existing([self.ACCOUNT])
        self.assertFalse(result['ok'])
        self.assertEqual(result['marked'], 0)

    def test_ensure_state_marks_a_fresh_account_as_new(self):
        self.warmup.ensure_state(self.ACCOUNT)
        state = self.warmup.load_state(self.ACCOUNT)
        self.assertEqual(state['readiness'], 0)
        self.assertIsNotNone(state['started_at'])
        self.assertEqual(self.warmup.tier_for(self.ACCOUNT), self.warmup.TIER_NEW)

    def test_ensure_state_does_not_overwrite_progress(self):
        self.warmup.save_state(self.ACCOUNT, {'readiness': 30, 'cycles': 3, 'started_at': 'x'})
        self.warmup.ensure_state(self.ACCOUNT)
        self.assertEqual(self.warmup.load_state(self.ACCOUNT)['readiness'], 30)

    def test_ensure_state_keeps_a_new_account_out_of_backfill(self):
        """上传时就写了状态，启动打标才不会把刚上传的号误当存量账号。"""
        self.warmup.ensure_state(self.ACCOUNT)
        self.assertEqual(self.warmup.backfill_existing([self.ACCOUNT])['marked'], 0)


class WarmupLoopTests(unittest.IsolatedAsyncioTestCase):
    """预热循环必须认得「账号已经被删了」，否则会对着不存在的 session 一直空转。"""

    async def test_account_deleted_midway_stops_the_loop(self):
        from app.services import warmup
        tmp = tempfile.TemporaryDirectory()
        previous = main.CONFIG.SESSION_DIR
        main.CONFIG.SESSION_DIR = tmp.name
        try:
            progress = {'running': True, 'started_at': None, 'finished_at': None,
                        'accounts': {}, 'current': None}
            with patch.object(warmup, '_PROGRESS', progress), \
                 patch.object(warmup, '_WARMUP_TASK', {'handle': None, 'stop': False}), \
                 patch.object(main.CONFIG, 'WARMUP_CYCLE_INTERVAL_S', 1):
                # session 文件不存在：这一批应该立刻结束，而不是每轮对着空 session 重试
                await asyncio.wait_for(warmup._warm_loop(None, ['ghost_account']), timeout=5)
            self.assertTrue(progress['accounts']['ghost_account']['removed'])
            self.assertFalse(progress['running'])
        finally:
            main.CONFIG.SESSION_DIR = previous
            tmp.cleanup()


class WarmupBatchTests(unittest.TestCase):
    """页面一次轮询几十个号：状态必须批量查，不能一个号开一次库。"""

    def setUp(self):
        from app.database import SessionLocal
        from app.models import SystemKV
        from app.services import warmup
        self.SessionLocal, self.SystemKV = SessionLocal, SystemKV
        self.warmup = warmup
        # 测试环境默认关闭预热，这里显式打开
        self._enabled = patch.object(main.CONFIG, 'WARMUP_ENABLED', 1)
        self._enabled.start()
        self._clear()

    def tearDown(self):
        self._enabled.stop()
        self._clear()

    def _clear(self):
        db = self.SessionLocal()
        try:
            db.query(self.SystemKV).filter(
                self.SystemKV.k.like(self.warmup.WARMUP_KEY_PREFIX + '%')
            ).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def test_load_states_matches_single_reads(self):
        self.warmup.save_state('batch_a', {'readiness': 30, 'cycles': 2})
        states = self.warmup.load_states(['batch_a', 'batch_b'])
        self.assertEqual(states['batch_a']['readiness'], 30)
        self.assertEqual(states['batch_b']['readiness'], 0)

    def test_pending_and_gate_only_return_ungraduated(self):
        self.warmup.save_state('batch_new', {'readiness': 0})
        self.warmup.save_state('batch_done', {'readiness': self.warmup.ready_score(),
                                             'graduated_at': 'x'})
        self.assertEqual(self.warmup.pending_accounts(['batch_new', 'batch_done']), ['batch_new'])
        self.assertEqual(self.warmup.gate_many(['batch_new', 'batch_done']), {'batch_done'})


class AccountAliveMarkTests(unittest.TestCase):
    """预热读通 = 登录有效；但不能把「发不了言/被封」的结论覆盖掉。"""

    def _clear(self):
        for name in ('alive_acct', 'alive_banned'):
            for flag in (False, True):
                main.account_service._cache.pop((name, flag), None)
            main._ACCOUNT_SEND_BLOCK.pop(name, None)

    def setUp(self):
        self._clear()

    def tearDown(self):
        self._clear()

    def test_unchecked_account_becomes_ok(self):
        main.account_service.mark_account_alive('alive_acct')
        self.assertEqual(main.account_service.cached_status('alive_acct')['status'], 'ok')

    def test_send_banned_account_is_not_revived(self):
        main.account_service.mark_account_send_blocked('alive_banned', 'spam ban')
        main.account_service.mark_account_alive('alive_banned')
        self.assertEqual(main.account_service.cached_status('alive_banned')['status'], 'send_banned')

    def test_health_check_does_not_clear_send_ban(self):
        """体检能连上 ≠ 能发言：限制发言的冻结不能被检测结果刷掉。"""
        import asyncio
        import time
        main.account_service.mark_account_send_blocked('alive_banned', 'spam ban')
        main._ACCOUNT_SEND_BLOCK['alive_banned'] = time.time() + 3600
        main._SEND_BLOCK_LOADED = True
        ok_result = {'account': 'alive_banned', 'status': 'ok', 'valid': True}
        with patch.object(main.account_service, '_check_account',
                          new=AsyncMock(return_value=ok_result)), \
                patch.object(main.account_service, '_persist_status'):
            result = asyncio.run(main.account_service.check_account('alive_banned'))
        self.assertEqual(result['status'], 'send_banned')
        self.assertFalse(result['can_send_in_groups'])
        self.assertEqual(main.account_service.cached_status('alive_banned')['status'], 'send_banned')


class SendBlockExpiryTests(unittest.TestCase):
    """限制发言的冷却到期要把账号放回池子，不能一直停在「已停止」。"""

    name = 'acct_expire_block'

    def setUp(self):
        import time
        main.account_service._cache.pop((self.name, False), None)
        main._ACCOUNT_SEND_BLOCK.pop(self.name, None)
        self._persist = patch.object(main.account_service, '_persist_status')
        self._persist.start()
        self.now = time.time

    def tearDown(self):
        self._persist.stop()
        main.account_service._cache.pop((self.name, False), None)
        main._ACCOUNT_SEND_BLOCK.pop(self.name, None)

    def test_expired_block_returns_account_to_pool(self):
        main.account_service.mark_account_send_blocked(self.name, 'spam ban')
        self.assertEqual(main.account_service.cached_status(self.name)['status'], 'send_banned')

        main._ACCOUNT_SEND_BLOCK[self.name] = self.now() - 1
        main._SEND_BLOCK_LOADED = True  # 别再去 DB 重读，覆盖掉手工塞的过期时间
        self.assertFalse(main._account_send_blocked(self.name))
        self.assertEqual(main.account_service.cached_status(self.name)['status'], 'ok')

    def test_active_block_keeps_account_out(self):
        main.account_service.mark_account_send_blocked(self.name, 'spam ban')
        main._ACCOUNT_SEND_BLOCK[self.name] = self.now() + 3600
        main._SEND_BLOCK_LOADED = True
        self.assertTrue(main._account_send_blocked(self.name))
        self.assertEqual(main.account_service.cached_status(self.name)['status'], 'send_banned')


class PipelineRecheckMessageTests(unittest.IsolatedAsyncioTestCase):
    """重检提示要说人话：预热未毕业的账号重检也救不回来。"""

    @staticmethod
    def _run() -> dict:
        return {'stop': False, 'stage': 'plan',
                'steps': [{'key': 'plan', 'label': '生成智能方案', 'status': 'running', 'detail': ''}]}

    async def test_warming_only_reports_instead_of_rechecking(self):
        run = self._run()
        checked: list[str] = []

        async def fake_check(name, **kwargs):
            checked.append(name)
            return {}

        with patch.object(main, '_candidate_accounts', return_value=['acc_a', 'acc_b']), \
             patch.object(main, '_blocked_account_names', return_value=['acc_a', 'acc_b']), \
             patch.object(main.warmup, 'gate_many', return_value=set()), \
             patch.object(main.account_service, 'check_account', side_effect=fake_check):
            await main._pipeline_recheck_step(run)

        self.assertEqual(checked, [], '预热未毕业的账号不该被重检，重检也救不回来')
        self.assertIn('预热未毕业', run['detail'])
        self.assertIn('重检无效', run['detail'])

    async def test_only_non_warming_accounts_are_rechecked(self):
        run = self._run()
        checked: list[str] = []

        async def fake_check(name, **kwargs):
            checked.append(name)
            return {}

        with patch.object(main, '_candidate_accounts', return_value=['warm_one', 'stale_one']), \
             patch.object(main, '_blocked_account_names', return_value=['warm_one', 'stale_one']), \
             patch.object(main.warmup, 'gate_many', return_value={'stale_one'}), \
             patch.object(main.account_service, 'check_account', side_effect=fake_check):
            await main._pipeline_recheck_step(run)

        self.assertEqual(checked, ['stale_one'])
        self.assertIn('预热未毕业', run['detail'])

class FloodWaitParsingTests(unittest.TestCase):
    """Telegram 限流的原文里没有 "FloodWait" 这个词，判断条件必须认「A wait of」。"""

    def test_recognises_telegram_wording(self):
        from app.services.send_service import _flood_wait_seconds
        # 下面两条是 8002 实测抓到的原文，就是要能认出来
        self.assertEqual(_flood_wait_seconds('A wait of 2956 seconds is required'), 2956)
        self.assertEqual(_flood_wait_seconds('A wait of 235 seconds is required'), 235)
        # 老写法（异常类名 + 秒数）也得继续认
        self.assertEqual(_flood_wait_seconds('FloodWaitError: 120'), 120)

    def test_other_errors_are_not_flood(self):
        from app.services.send_service import _flood_wait_seconds
        for err in ('user_banned_in_channel', 'chat_write_forbidden',
                    'The channel specified is private', '', None):
            self.assertIsNone(_flood_wait_seconds(err), err)


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    """令牌桶按账号隔离：一个账号被限住不该拖到别的账号。"""

    async def test_zero_rate_returns_immediately(self):
        from app.services.ratelimit import TokenBucket
        await asyncio.wait_for(TokenBucket(0).acquire(), timeout=1)

    async def test_first_token_is_free(self):
        from app.services.ratelimit import TokenBucket
        await asyncio.wait_for(TokenBucket(1.0).acquire(), timeout=1)

    async def test_second_token_has_to_wait(self):
        import time as _time
        from app.services.ratelimit import TokenBucket
        bucket = TokenBucket(20.0)
        await bucket.acquire()
        started = _time.monotonic()
        await bucket.acquire()
        self.assertGreaterEqual(_time.monotonic() - started, 0.03)

    def test_rate_is_per_account(self):
        from app.services.ratelimit import RateLimiter
        limiter = RateLimiter(default_rate=1.0)
        limiter.set_rate('slow', 0.5)
        self.assertEqual(limiter.rate_for('slow'), 0.5)
        self.assertEqual(limiter.rate_for('other'), 1.0)

    def test_forget_drops_the_override(self):
        from app.services.ratelimit import RateLimiter
        limiter = RateLimiter(default_rate=1.0)
        limiter.set_rate('gone', 0.5)
        limiter.forget('gone')
        self.assertEqual(limiter.rate_for('gone'), 1.0)


class PipelineRunStatsTests(unittest.TestCase):
    """本次执行的累计：只数「开始 → 停止」之间的日志，停止后封口不再增长。"""

    account = 'acct_run_stats'

    def setUp(self):
        from app.database import SessionLocal
        from app.models import JoinLog, SendLog
        self.SessionLocal, self.SendLog, self.JoinLog = SessionLocal, SendLog, JoinLog
        self._clear()

    def tearDown(self):
        self._clear()

    def _clear(self):
        db = self.SessionLocal()
        try:
            db.query(self.SendLog).filter(self.SendLog.account_name == self.account).delete(synchronize_session=False)
            db.query(self.JoinLog).filter(self.JoinLog.account_name == self.account).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def _add(self, kind, seconds_ago, status):
        from datetime import timedelta
        db = self.SessionLocal()
        try:
            model = self.SendLog if kind == 'send' else self.JoinLog
            extra = {'message_preview': 'x'} if kind == 'send' else {}
            db.add(model(account_name=self.account, group_id=-8801, status=status,
                         created_at=main.CONFIG.now() - timedelta(seconds=seconds_ago),
                         **extra))
            db.commit()
        finally:
            db.close()

    def test_counts_only_inside_window(self):
        from datetime import timedelta
        now = main.CONFIG.now()
        self._add('send', 100, 'success')
        self._add('send', 1000, 'success')   # 上一次执行的，不该算进来
        self._add('send', 100, 'failed')     # 失败不算发出去
        self._add('join', 100, 'joined')
        self._add('join', 100, 'skipped')    # 本来就在群里，不算新加
        stats = main._pipeline_run_stats({
            'started_at': (now - timedelta(seconds=150)).isoformat(),
            'finished_at': (now - timedelta(seconds=50)).isoformat(),
        })
        self.assertEqual(stats['send_success'], 1)
        self.assertEqual(stats['send_failed'], 1)
        self.assertEqual(stats['join_success'], 1)

    def test_finished_run_stops_counting(self):
        from datetime import timedelta
        now = main.CONFIG.now()
        self._add('send', 100, 'success')  # 停止之后才发出去的
        stats = main._pipeline_run_stats({
            'started_at': (now - timedelta(seconds=300)).isoformat(),
            'finished_at': (now - timedelta(seconds=200)).isoformat(),
        })
        self.assertEqual(stats['send_success'], 0)

    def test_no_run_returns_zero(self):
        self.assertEqual(main._pipeline_run_stats(None),
                         {'send_success': 0, 'send_failed': 0, 'join_success': 0})

    def test_run_window_is_half_open_when_running(self):
        """还在跑时只有起点、没有终点：日志持续可查。"""
        from datetime import timedelta
        now = main.CONFIG.now()
        start, end = main._pipeline_run_window({'started_at': (now - timedelta(minutes=5)).isoformat()})
        self.assertIsNone(end)
        self.assertLess(abs((now.replace(tzinfo=None) - start).total_seconds() - 300), 5)


class RoundLoopTests(unittest.IsolatedAsyncioTestCase):
    """轮次推进：动态任务不能因为「本轮没群可发」就判完成，也不能硬睡一整个轮间隔。"""

    async def asyncSetUp(self):
        from app.database import SessionLocal
        from app.models import SendLog, Task
        self.SessionLocal = SessionLocal
        self.Task = Task
        self.SendLog = SendLog

    def _add_task(self, task_id, account, **fields):
        db = self.SessionLocal()
        try:
            db.add(self.Task(id=task_id, status='running', total=0, success=0, failed=0,
                             account_name=account, message='x', group_ids_json='[]',
                             delay_ms=0, **fields))
            db.commit()
        finally:
            db.close()

    def _drop(self, task_id, account):
        db = self.SessionLocal()
        try:
            db.query(self.SendLog).filter(self.SendLog.account_name == account).delete(synchronize_session=False)
            db.query(self.Task).filter(self.Task.id == task_id).delete()
            db.commit()
        finally:
            db.close()

    def _get(self, task_id):
        db = self.SessionLocal()
        try:
            return db.query(self.Task).filter(self.Task.id == task_id).one()
        finally:
            db.close()

    async def test_dynamic_task_without_groups_waits_instead_of_completing(self):
        """账号一个群都没有时只能等（1 分钟后重算群池），判完成会被常驻模式反复重建。"""
        task_id = 't-round-wait'
        self._add_task(task_id, 'acct_no_groups', dynamic_groups=1, rounds=100,
                       current_round=1, round_interval_s=600, group_msg_cooldown_s=0)
        sleeps = []

        async def fake_sleep(tid, seconds, **kwargs):
            sleeps.append(seconds)
            return False  # 立刻退出，模拟任务被停止

        try:
            with patch.object(main, 'get_banned_group_ids', return_value=[]), \
                    patch.object(main, '_account_send_blocked', return_value=False), \
                    patch.object(main, '_sleep_with_task_checks', fake_sleep):
                await main._run_send_task(task_id, 'acct_no_groups', [], 'x', 'plain', True, 0, 100, 600)
            row = self._get(task_id)
            self.assertEqual(row.status, 'running')
            self.assertIsNotNone(row.next_round_at)
            self.assertEqual(sleeps, [60.0])
        finally:
            self._drop(task_id, 'acct_no_groups')
            main.TASKS.pop(task_id, None)

    async def test_rounds_keep_going_until_rounds_are_used_up(self):
        """两轮都该真的跑起来，并且跑完判 done，不会卡在第一轮的等待里。"""
        task_id = 't-round-two'
        account = 'acct_two_rounds'
        self._add_task(task_id, account, dynamic_groups=0, rounds=2,
                       current_round=1, round_interval_s=0, group_msg_cooldown_s=0)
        sent = []

        async def fake_send(acc, group_id=None, **kwargs):
            sent.append(group_id)
            return True, None, 1

        try:
            with patch.object(main, 'get_banned_group_ids', return_value=[]), \
                    patch.object(main, '_account_send_blocked', return_value=False), \
                    patch.object(main, '_compute_effective_task_delay_ms', return_value=0), \
                    patch.object(main.multi_manager, 'send_message_to_group', fake_send), \
                    patch.object(main.multi_manager, 'get_group_title', AsyncMock(return_value='t')):
                await main._run_send_task(task_id, account, [-70001], 'x', 'plain', True, 0, 2, 0)
            row = self._get(task_id)
            self.assertEqual(row.status, 'done')
            self.assertEqual(row.current_round, 2)
            self.assertEqual(sent, [-70001, -70001])
        finally:
            self._drop(task_id, account)
            main.TASKS.pop(task_id, None)


if __name__ == '__main__':
    unittest.main()
