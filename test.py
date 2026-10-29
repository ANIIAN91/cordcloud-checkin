import base64
import hashlib
import json
import unittest
from pathlib import Path
from unittest import mock
import uuid
import requests

from app.action import Action, AuthError, RetryableError
from app.browser import BrowserError, load_session
from app.headless import HeadlessError, prepare_login, _cap_reason
from app.config import get_or_create_device_fingerprint, load_config
from app.notify import NotifyError, TelegramNotifier


class FakeResponse:
    def __init__(self, url, text='', json_data=None, headers=None, status_code=200):
        self.url = url
        self.text = text
        self._json_data = json_data
        self.headers = headers or {}
        self.status_code = status_code

    def json(self):
        if self._json_data is None:
            raise ValueError('no json')
        return self._json_data


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.headers = {}
        self.calls = []
        self.cookies = requests.cookies.RequestsCookieJar()

    def _pop(self, method, url):
        key = (method, url)
        if key not in self.responses or not self.responses[key]:
            raise AssertionError(f'unexpected {method} {url}')
        return self.responses[key].pop(0)

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        return self._pop('GET', url)

    def post(self, url, data=None, **kwargs):
        self.calls.append(('POST', url, {'data': data, **kwargs}))
        return self._pop('POST', url)


def build_challenge(number=7, salt='salt?expires=9999999999&', algorithm='SHA-256'):
    challenge = hashlib.sha256(f'{salt}{number}'.encode('utf-8')).hexdigest()
    return {
        'algorithm': algorithm,
        'challenge': challenge,
        'maxnumber': 50,
        'salt': salt,
        'signature': 'signed',
    }


class ActionTests(unittest.TestCase):
    def test_cap_response_reason_only_preserves_public_codes(self):
        self.assertEqual(_cap_reason({'code': 'instr_blocked', 'token': 'secret'}), 'instr_blocked')
        self.assertEqual(_cap_reason({'error': 'automated_browser'}), 'automated_browser')
        for payload in ({'code': 'user@example.com'}, {'error': {'token': 'secret'}},
                        {'reason': '::error::injected'}, None, ['secret']):
            self.assertEqual(_cap_reason(payload), 'redacted_unknown')

    def test_cap_rejection_is_sanitized_and_profile_is_removed(self):
        for reason in ('automated_browser', 'secret_account_id', None):
            with self.subTest(reason=reason), mock.patch('app.headless.log.info') as info, \
                    mock.patch('patchright.sync_api.sync_playwright') as playwright:
                launch = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
                context = launch.return_value
                context.browser.version = '156.0.8078.47'
                context.pages = []
                page = context.new_page.return_value
                page.url = 'https://cordcloud.biz/auth/login?token=url-secret'
                page.evaluate.return_value = {'cap_endpoint': 'https://cap.example/site/'}
                response = mock.Mock(url='https://cap.example/site/redeem?token=response-secret', status=403)
                if reason is None:
                    response.json.side_effect = ValueError('body-secret')
                else:
                    response.json.return_value = {'error': reason, 'token': 'body-secret'}

                def navigate(*args, **kwargs):
                    page.on.call_args.args[1](response)
                    return mock.Mock(status=200)

                page.goto.side_effect = navigate
                with self.assertRaises(HeadlessError) as error:
                    prepare_login(page.url)
                output = '\n'.join(call.args[0] for call in info.call_args_list) + str(error.exception)
                self.assertIn('http_status=403', output)
                self.assertIn('code=' + ('automated_browser' if reason == 'automated_browser'
                                        else 'redacted_unknown'), output)
                for secret in ('secret_account_id', 'body-secret', 'response-secret', 'url-secret', '::error::'):
                    self.assertNotIn(secret, output)
                context.cookies.assert_not_called()
                context.close.assert_called_once()
                self.assertFalse(Path(launch.call_args.kwargs['user_data_dir']).exists())
                self.assertEqual(page.on.call_args.args[0], 'response')

    def test_cap_timeout_and_browser_errors_log_stage_without_raw_exception(self):
        from patchright.sync_api import Error
        for failure in ('timeout', 'browser_error', 'launch_error'):
            with self.subTest(failure=failure), mock.patch('app.headless.log.info') as info, \
                    mock.patch('patchright.sync_api.sync_playwright') as playwright:
                launch = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
                context = launch.return_value
                context.browser.version = '156.0.8078.47'
                context.pages = []
                page = context.new_page.return_value
                page.url = 'https://cordcloud.biz/auth/login'
                page.goto.return_value.status = 200
                if failure == 'browser_error':
                    page.goto.side_effect = Error('https://example.com/?token=exception-secret')
                if failure == 'launch_error':
                    launch.side_effect = Error('profile-secret')
                with self.assertRaises(HeadlessError):
                    prepare_login(page.url, timeout=0)
                output = '\n'.join(call.args[0] for call in info.call_args_list)
                self.assertIn('reason=cap_wait_timeout' if failure == 'timeout'
                              else 'reason=browser_error', output)
                self.assertNotIn('exception-secret', output)
                self.assertNotIn('profile-secret', output)
                if failure != 'launch_error':
                    context.close.assert_called_once()
                self.assertFalse(Path(launch.call_args.kwargs['user_data_dir']).exists())

    def test_cap_failure_does_not_submit_password(self):
        session = FakeSession({
            ('GET', 'https://cordcloud.biz/auth/login'): [
                FakeResponse('https://cordcloud.biz/auth/login', text='<cap-widget></cap-widget>'),
            ],
        })
        action = Action('user@example.com', 'passwd', host='cordcloud.biz', session=session)
        with mock.patch('app.headless.prepare_login', side_effect=HeadlessError('Cap 验证失败')):
            with self.assertRaisesRegex(RetryableError, 'Cap'):
                action.login()
        self.assertEqual([call[0] for call in session.calls], ['GET'])

    def test_cap_session_handoff_completes_2fa_and_checkin(self):
        session = FakeSession({
            ('GET', 'https://cordcloud.biz/auth/login'): [
                FakeResponse('https://cordcloud.biz/auth/login', text='<cap-widget></cap-widget>'),
            ],
            ('POST', 'https://cordcloud.biz/auth/login'): [
                FakeResponse('https://cordcloud.biz/auth/login', json_data={
                    'ret': 2, 'need_device_2fa': True, 'methods': {'ga': True}, 'token': '2fa-token',
                }),
            ],
            ('POST', 'https://cordcloud.biz/auth/login/2fa/verify'): [
                FakeResponse('https://cordcloud.biz/auth/login/2fa/verify', json_data={'ret': 1}),
            ],
            ('GET', 'https://cordcloud.biz/user'): [
                FakeResponse('https://cordcloud.biz/user', text='<input name="csrf_token" value="checkin-csrf">'),
            ],
            ('POST', 'https://cordcloud.biz/user/checkin'): [
                FakeResponse('https://cordcloud.biz/user/checkin', json_data={'ret': 1}),
            ],
        })
        session.cookies.set('old-session', 'old', domain='cordcloud.biz')
        data = {'cap_token': 'cap-token', 'csrf_token': 'browser-csrf',
                'device_fingerprint': 'browser-fingerprint', 'user_agent': 'Chromium UA'}
        cookies = [{'name': 'session', 'value': 'new', 'domain': 'cordcloud.biz',
                    'path': '/', 'secure': True, 'expires': 2000000000, 'httpOnly': True},
                   {'name': 'scoped', 'value': 'auth-only', 'domain': 'cordcloud.biz',
                    'path': '/auth', 'secure': True, 'expires': -1}]
        action = Action('user@example.com', 'passwd', host='cordcloud.biz', session=session,
                        secret='JBSWY3DPEHPK3PXP', verify_method='ga')
        with mock.patch('app.headless.prepare_login', return_value=(data, cookies)):
            with mock.patch.object(action, '_current_code', return_value='123456'):
                self.assertEqual(action.login()['ret'], 1)
                self.assertEqual(action.check_in()['ret'], 1)
        self.assertIsNone(session.cookies.get('old-session'))
        self.assertEqual(session.cookies.get('session'), 'new')
        self.assertEqual(session.headers['User-Agent'], 'Chromium UA')
        payload = session.calls[1][2]['data']
        self.assertEqual(payload['cap_token'], 'cap-token')
        self.assertEqual(payload['csrf_token'], 'browser-csrf')
        self.assertEqual(payload['device_fingerprint'], 'browser-fingerprint')
        self.assertEqual(session.calls[2][2]['data']['code'], '123456')
        self.assertEqual(session.calls[2][2]['data']['trust_device'], '0')
        self.assertEqual(session.calls[4][2]['data'], {'csrf_token': 'checkin-csrf'})
        self.assertEqual(session.calls[1][2]['headers']['Referer'], 'https://cordcloud.biz/auth/login')
        saved = {cookie.name: cookie for cookie in session.cookies}
        self.assertEqual(saved['session'].expires, 2000000000)
        self.assertTrue(saved['session'].secure)
        self.assertTrue(saved['session'].get_nonstandard_attr('HttpOnly'))
        self.assertEqual(saved['scoped'].path, '/auth')
        self.assertIsNone(saved['scoped'].expires)

    def test_incomplete_cap_session_never_submits_credentials_or_clears_old_cookie(self):
        session = FakeSession({
            ('GET', 'https://cordcloud.biz/auth/login'): [
                FakeResponse('https://cordcloud.biz/auth/login', text='<cap-widget></cap-widget>'),
            ],
        })
        session.cookies.set('old-session', 'old', domain='cordcloud.biz')
        action = Action('user@example.com', 'passwd', host='cordcloud.biz', session=session)
        with mock.patch('app.headless.prepare_login', return_value=({'cap_token': 'secret'}, [])):
            with self.assertRaises(RetryableError):
                action.login()
        self.assertEqual([call[0] for call in session.calls], ['GET'])
        self.assertEqual(session.cookies.get('old-session'), 'old')

    def test_headless_widget_wait_and_cleanup(self):
        with mock.patch('patchright.sync_api.sync_playwright') as playwright:
            context = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
            context.browser.version = '156.0.8078.47'
            context.pages = []
            page = context.new_page.return_value
            page.url = 'https://cordcloud.biz/auth/login'
            page.goto.return_value.status = 200
            ready = {'cap_token': 'cap-secret-value', 'csrf_token': 'csrf-secret-value',
                     'device_fingerprint': 'fingerprint-secret-value',
                     'user_agent': 'Chromium UA'}

            def navigate(*args, **kwargs):
                unrelated = mock.Mock(url='https://other.example/site/redeem?token=unrelated-secret', status=403)
                page.on.call_args.args[1](unrelated)
                return mock.Mock(status=200)

            page.goto.side_effect = navigate
            page.evaluate.side_effect = [
                {'cap_endpoint': 'https://cap.example/site/'},
                dict(ready, cap_endpoint='https://cap.example/site/'),
            ]
            context.cookies.return_value = [{'name': 'session', 'value': 'cookie-secret-value'}]
            with mock.patch('app.headless.log.info') as info:
                data, cookies = prepare_login(page.url)
            output = '\n'.join(call.args[0] for call in info.call_args_list)
            self.assertIn('cap_token_present=false', output)
            self.assertIn('cap_token_present=true', output)
            self.assertIn('stage=collect_session event=ready', output)
            self.assertNotIn('Chromium UA', output)
            for secret in (*ready.values(), 'cookie-secret-value', 'unrelated-secret'):
                self.assertNotIn(secret, output)
            self.assertEqual(data, ready)
            self.assertEqual(cookies[0]['name'], 'session')
            page.wait_for_function.assert_not_called()
            page.locator.assert_not_called()
            self.assertFalse(page.evaluate.call_args.kwargs['isolated_context'])
            context.cookies.assert_called_once_with(['https://cordcloud.biz/auth/login'])
            context.close.assert_called_once()

    def test_headless_rejects_redirect_http_error_and_timeout(self):
        for url, status, timeout in (
            ('https://other.example/auth/login', 200, 10),
            ('https://cordcloud.biz/auth/login', 503, 10),
            ('https://cordcloud.biz/auth/login', 200, 0),
        ):
            with self.subTest(url=url, status=status, timeout=timeout):
                with mock.patch('patchright.sync_api.sync_playwright') as playwright:
                    context = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value
                    context.browser.version = '156.0.8078.47'
                    context.pages = []
                    page = context.new_page.return_value
                    page.url = url
                    page.goto.return_value.status = status
                    with self.assertRaises(HeadlessError):
                        prepare_login('https://cordcloud.biz/auth/login', timeout=timeout)
                    context.close.assert_called_once()

    def test_browser_session_is_verified_before_checkin(self):
        session = FakeSession({
            ('GET', 'https://cordcloud.biz/user'): [
                FakeResponse('https://cordcloud.biz/user', text='<div>账号中心</div>'),
                FakeResponse('https://cordcloud.biz/user', text='<div>账号中心</div>'),
            ],
            ('POST', 'https://cordcloud.biz/user/checkin'): [
                FakeResponse('https://cordcloud.biz/user/checkin', json_data={'ret': 1, 'msg': '签到成功'}),
            ],
        })
        action = Action('', '', host='cordcloud.biz', session=session, browser_login=True)
        cookies = [{'name': 'session', 'value': 'test-session', 'domain': 'cordcloud.biz'}]
        with mock.patch('app.browser.load_session', return_value=('Browser UA', cookies)):
            self.assertEqual(action.login()['ret'], 1)
        self.assertEqual(session.cookies.get('session'), 'test-session')
        self.assertEqual(session.headers['User-Agent'], 'Browser UA')
        self.assertEqual(action.check_in()['ret'], 1)
        self.assertFalse(any('/auth/' in call[1] for call in session.calls))

    def test_browser_expired_or_failed_session_is_not_success(self):
        for response in (
            FakeResponse('https://cordcloud.biz/auth/login'),
            FakeResponse('https://cordcloud.biz/user', status_code=503),
            FakeResponse('https://cordcloud.biz/user', text="<form id='login-form'>"),
        ):
            with self.subTest(response=response):
                session = FakeSession({('GET', 'https://cordcloud.biz/user'): [response]})
                action = Action('', '', host='cordcloud.biz', session=session, browser_login=True)
                with mock.patch('app.browser.load_session', return_value=('Browser UA', [])):
                    with self.assertRaises(RetryableError):
                        action.login()
                self.assertEqual([call[0] for call in session.calls], ['GET'])

    def test_browser_only_reads_requested_site_and_keeps_tabs(self):
        tab = {'url': 'https://cordcloud.biz/user', 'tab_id': '123',
               'browser_id': 'browser', 'profile_id': 'profile', 'session_key': 'session'}
        replies = [
            {'metadata': {'tabs': [dict(tab, url='https://other.example/user'), tab]}},
            {'js_return': {'url': tab['url'], 'user_agent': 'Browser UA', 'login_form': False}},
            {'js_return': {'cookies': [{'name': 'session', 'value': 'test'}]}},
        ]
        with mock.patch('app.browser._call', side_effect=replies) as call:
            ua, cookies = load_session(tab['url'])
        self.assertEqual(ua, 'Browser UA')
        self.assertEqual(len(cookies), 1)
        cdp = json.loads(call.call_args_list[-1].args[1])
        self.assertEqual(cdp['params']['urls'], ['https://cordcloud.biz/user'])
        self.assertEqual(cdp['sessionKey'], 'session')
        self.assertEqual([c.args[0] for c in call.call_args_list], ['tabs', 'exec', 'exec'])

    def test_browser_missing_or_ambiguous_account_requires_user_login(self):
        tab = {'url': 'https://cordcloud.biz/user', 'profile_id': 'one'}
        for tabs in ([], [tab, dict(tab, profile_id='two')]):
            with self.subTest(tabs=tabs):
                with mock.patch('app.browser._call', return_value={'metadata': {'tabs': tabs}}) as call:
                    with self.assertRaises(BrowserError):
                        load_session('https://cordcloud.biz/user')
                call.assert_called_once_with('tabs')

    def test_load_config_merges_default_and_local_files(self):
        tmp = Path('.test-config-tmp') / str(uuid.uuid4())
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            default_path = tmp / 'config.default.json'
            local_path = tmp / 'config.local.json'
            default_path.write_text(json.dumps({
                'email': '',
                'passwd': '',
                'secret': '',
                'code': '',
                'verify_method': '',
                'host': 'cordcloud.one',
                'trust_device': 'false',
                'insecure_skip_verify': 'false',
                'telegram_bot_token': '',
                'telegram_chat_id': '',
                'device_fingerprint': '',
            }), encoding='utf-8')
            local_path.write_text(json.dumps({
                'email': 'user@example.com',
                'passwd': 'passwd',
                'code': '123456',
            }), encoding='utf-8')

            config = load_config((default_path, local_path))
        finally:
            if local_path.exists():
                local_path.unlink()
            if default_path.exists():
                default_path.unlink()
            tmp.rmdir()

        self.assertEqual(config['email'], 'user@example.com')
        self.assertEqual(config['passwd'], 'passwd')
        self.assertEqual(config['code'], '123456')
        self.assertEqual(config['host'], 'cordcloud.one')

    def test_device_fingerprint_is_persisted_only_in_local_config(self):
        tmp = Path('.test-config-tmp') / str(uuid.uuid4())
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            local_path = tmp / 'config.local.json'
            local_path.write_text('{}', encoding='utf-8')
            config = load_config((local_path,))

            fingerprint = get_or_create_device_fingerprint(config, local_path=local_path)
            saved = json.loads(local_path.read_text(encoding='utf-8'))

            self.assertEqual(saved['device_fingerprint'], fingerprint)
            self.assertEqual(get_or_create_device_fingerprint(config, local_path=local_path), fingerprint)
        finally:
            if local_path.exists():
                local_path.unlink()
            tmp.rmdir()

    def test_login_submits_csrf_altcha_and_fingerprint(self):
        login_html = '''
        <form action="javascript:void(0);" method="POST" id="login-form">
            <input type="hidden" name="csrf_token" value="csrf-1">
            <input type="email" id="email" name="Email" value="">
            <input type="password" id="passwd" name="Password" value="">
            <altcha-widget challengeurl="/auth/altcha/challenge"></altcha-widget>
        </form>
        '''
        challenge = build_challenge()
        session = FakeSession({
            ('GET', 'https://cordcloud.one/auth/login'): [
                FakeResponse('https://cordcloud.one/auth/login', text=login_html),
            ],
            ('GET', 'https://cordcloud.one/auth/altcha/challenge'): [
                FakeResponse('https://cordcloud.one/auth/altcha/challenge', json_data=challenge),
            ],
            ('POST', 'https://cordcloud.one/auth/login'): [
                FakeResponse('https://cordcloud.one/auth/login', json_data={'ret': 1, 'msg': '登录成功'}),
            ],
        })

        action = Action('user@example.com', 'passwd', host='cordcloud.one', session=session)
        result = action.login()

        self.assertEqual(result['ret'], 1)
        _, _, post_kwargs = session.calls[-1]
        form_data = post_kwargs['data']
        self.assertEqual(form_data['email'], 'user@example.com')
        self.assertEqual(form_data['passwd'], 'passwd')
        self.assertEqual(form_data['csrf_token'], 'csrf-1')
        self.assertEqual(form_data['device_fingerprint'], action.device_fingerprint)
        self.assertTrue(post_kwargs['verify'])

        payload = json.loads(base64.b64decode(form_data['altcha']).decode('utf-8'))
        self.assertEqual(payload['challenge'], challenge['challenge'])
        self.assertEqual(payload['number'], 7)

    def test_login_handles_device_2fa(self):
        login_html = '''
        <form action="javascript:void(0);" method="POST" id="login-form">
            <input type="hidden" name="csrf_token" value="csrf-1">
            <altcha-widget challengeurl="/auth/altcha/challenge"></altcha-widget>
        </form>
        '''
        session = FakeSession({
            ('GET', 'https://cordcloud.one/auth/login'): [
                FakeResponse('https://cordcloud.one/auth/login', text=login_html),
            ],
            ('GET', 'https://cordcloud.one/auth/altcha/challenge'): [
                FakeResponse('https://cordcloud.one/auth/altcha/challenge', json_data=build_challenge()),
            ],
            ('POST', 'https://cordcloud.one/auth/login'): [
                FakeResponse(
                    'https://cordcloud.one/auth/login',
                    json_data={
                        'ret': 2,
                        'msg': '需要设备验证',
                        'need_device_2fa': True,
                        'methods': {'email': True, 'ga': True},
                        'token': 'abc',
                        'redirect': '/auth/login/2fa?token=abc',
                    },
                ),
            ],
            ('POST', 'https://cordcloud.one/auth/login/2fa/verify'): [
                FakeResponse('https://cordcloud.one/auth/login/2fa/verify', json_data={'ret': 1, 'msg': '登录成功'}),
            ],
        })

        action = Action('user@example.com', 'passwd', secret='JBSWY3DPEHPK3PXP', host='cordcloud.one', session=session)
        with mock.patch.object(Action, '_current_code', return_value='123456'):
            result = action.login()

        self.assertEqual(result['ret'], 1)
        _, _, post_kwargs = session.calls[-1]
        form_data = post_kwargs['data']
        self.assertEqual(form_data['code'], '123456')
        self.assertEqual(form_data['trust_device'], '0')
        self.assertEqual(form_data['token'], 'abc')
        self.assertEqual(form_data['method'], 'ga')
        self.assertTrue(post_kwargs['verify'])
        verify_get_calls = [call for call in session.calls if call[0] == 'GET' and '/auth/login/2fa?' in call[1]]
        self.assertEqual(verify_get_calls, [])

    def test_login_can_explicitly_trust_device(self):
        login_html = '''
        <form action="javascript:void(0);" method="POST" id="login-form">
            <input type="hidden" name="csrf_token" value="csrf-1">
            <altcha-widget challengeurl="/auth/altcha/challenge"></altcha-widget>
        </form>
        '''
        verify_html = '''
        <form action="javascript:void(0);" method="POST" id="verify-form">
            <input type="hidden" name="token" value="abc">
            <input type="hidden" name="method" id="verify-method" value="email">
            <input type="text" id="code" name="code" value="">
        </form>
        '''
        session = FakeSession({
            ('GET', 'https://cordcloud.one/auth/login'): [
                FakeResponse('https://cordcloud.one/auth/login', text=login_html),
            ],
            ('GET', 'https://cordcloud.one/auth/altcha/challenge'): [
                FakeResponse('https://cordcloud.one/auth/altcha/challenge', json_data=build_challenge()),
            ],
            ('POST', 'https://cordcloud.one/auth/login'): [
                FakeResponse(
                    'https://cordcloud.one/auth/login',
                    json_data={
                        'ret': 2,
                        'msg': '需要设备验证',
                        'need_device_2fa': True,
                        'methods': {'email': True},
                        'redirect': '/auth/login/2fa?token=abc',
                    },
                ),
            ],
            ('GET', 'https://cordcloud.one/auth/login/2fa?token=abc'): [
                FakeResponse('https://cordcloud.one/auth/login/2fa?token=abc', text=verify_html),
            ],
            ('POST', 'https://cordcloud.one/auth/login/2fa/verify'): [
                FakeResponse('https://cordcloud.one/auth/login/2fa/verify', json_data={'ret': 1, 'msg': '登录成功'}),
            ],
        })

        action = Action(
            'user@example.com',
            'passwd',
            code='123456',
            host='cordcloud.one',
            session=session,
            trust_device=True,
        )
        result = action.login()

        self.assertEqual(result['ret'], 1)
        _, _, post_kwargs = session.calls[-1]
        self.assertEqual(post_kwargs['data']['trust_device'], '1')

    def test_login_respects_explicit_email_verify_method(self):
        login_html = '''
        <form action="javascript:void(0);" method="POST" id="login-form">
            <input type="hidden" name="csrf_token" value="csrf-1">
            <altcha-widget challengeurl="/auth/altcha/challenge"></altcha-widget>
        </form>
        '''
        verify_html = '''
        <form action="javascript:void(0);" method="POST" id="verify-form">
            <input type="hidden" name="csrf_token" value="csrf-2">
            <input type="hidden" name="token" value="abc">
            <input type="hidden" name="method" id="verify-method" value="email">
            <input type="text" id="code" name="code" value="">
        </form>
        '''
        session = FakeSession({
            ('GET', 'https://cordcloud.one/auth/login'): [
                FakeResponse('https://cordcloud.one/auth/login', text=login_html),
            ],
            ('GET', 'https://cordcloud.one/auth/altcha/challenge'): [
                FakeResponse('https://cordcloud.one/auth/altcha/challenge', json_data=build_challenge()),
            ],
            ('POST', 'https://cordcloud.one/auth/login'): [
                FakeResponse(
                    'https://cordcloud.one/auth/login',
                    json_data={
                        'ret': 2,
                        'msg': '需要设备验证',
                        'need_device_2fa': True,
                        'methods': {'email': True, 'ga': True},
                        'token': 'abc',
                        'redirect': '/auth/login/2fa?token=abc',
                    },
                ),
            ],
            ('GET', 'https://cordcloud.one/auth/login/2fa?token=abc'): [
                FakeResponse('https://cordcloud.one/auth/login/2fa?token=abc', text=verify_html),
            ],
            ('POST', 'https://cordcloud.one/auth/login/2fa/verify'): [
                FakeResponse('https://cordcloud.one/auth/login/2fa/verify', json_data={'ret': 1, 'msg': '登录成功'}),
            ],
        })

        action = Action(
            'user@example.com',
            'passwd',
            secret='JBSWY3DPEHPK3PXP',
            code='654321',
            verify_method='email',
            host='cordcloud.one',
            session=session,
        )
        result = action.login()

        self.assertEqual(result['ret'], 1)
        _, _, post_kwargs = session.calls[-1]
        form_data = post_kwargs['data']
        self.assertEqual(form_data['method'], 'email')
        self.assertEqual(form_data['code'], '654321')

    def test_telegram_notifier_sends_message(self):
        notifier = TelegramNotifier(bot_token='bot-token', chat_id='123456')
        response = mock.Mock(status_code=200)
        response.json.return_value = {'ok': True, 'result': {'message_id': 1}}

        with mock.patch('app.notify.requests.post', return_value=response) as mocked_post:
            self.assertTrue(notifier.send('签到成功'))

        mocked_post.assert_called_once()
        _, kwargs = mocked_post.call_args
        self.assertEqual(kwargs['data']['chat_id'], '123456')
        self.assertEqual(kwargs['data']['text'], '签到成功')

    def test_telegram_notifier_raises_on_api_error(self):
        notifier = TelegramNotifier(bot_token='bot-token', chat_id='123456')
        response = mock.Mock(status_code=400)
        response.json.return_value = {'ok': False, 'description': 'chat not found'}

        with mock.patch('app.notify.requests.post', return_value=response):
            with self.assertRaises(NotifyError):
                notifier.send('签到失败')

    def test_check_in_uses_user_csrf_token(self):
        user_html = '''
        <div class="dashboard">
            <input type="hidden" name="csrf_token" value="csrf-user">
        </div>
        '''
        session = FakeSession({
            ('GET', 'https://cordcloud.one/user'): [
                FakeResponse('https://cordcloud.one/user', text=user_html),
            ],
            ('POST', 'https://cordcloud.one/user/checkin'): [
                FakeResponse('https://cordcloud.one/user/checkin', json_data={'ret': 1, 'msg': '签到成功'}),
            ],
        })

        action = Action('user@example.com', 'passwd', host='cordcloud.one', session=session)
        result = action.check_in()

        self.assertEqual(result['ret'], 1)
        _, _, post_kwargs = session.calls[-1]
        self.assertEqual(post_kwargs['data']['csrf_token'], 'csrf-user')


if __name__ == '__main__':
    unittest.main()
