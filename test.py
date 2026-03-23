import base64
import hashlib
import json
import unittest
from pathlib import Path
from unittest import mock
import uuid

from app.action import Action
from app.config import get_or_create_device_fingerprint, load_config


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
