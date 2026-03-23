import base64
import hashlib
import json
import re
import secrets
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from requests import exceptions as request_exceptions

try:
    import pyotp
except ImportError:  # pragma: no cover - runtime dependency is installed in the action image
    pyotp = None


class ActionError(RuntimeError):
    pass


class AuthError(ActionError):
    pass


class RetryableError(ActionError):
    pass


class Action:
    INPUT_TAG_RE = re.compile(r'<input\b([^>]*)>', re.I)
    ATTR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*([\'"])(.*?)\2', re.S)
    FORM_ACTION_RE = re.compile(r'<form\b[^>]*action\s*=\s*([\'"])(.*?)\1', re.I | re.S)
    ALTCHA_RE = re.compile(r'challengeurl\s*=\s*([\'"])(.*?)\1', re.I | re.S)

    def __init__(
        self,
        email: str,
        passwd: str,
        secret: str = '',
        code: str = '',
        verify_method: str = '',
        host: str = 'cordcloud.us',
        session: Optional[requests.Session] = None,
        trust_device: bool = False,
        verify_tls: bool = True,
        device_fingerprint: str = '',
    ):
        self.email = email
        self.passwd = passwd
        self.secret = secret
        self.code = code
        self.verify_method = verify_method.strip().lower()
        self.host = host.replace('https://', '').replace('http://', '').strip().rstrip('/')
        self.session = session or requests.session()
        self.timeout = 15
        self.trust_device = trust_device
        self.verify_tls = verify_tls
        self.device_fingerprint = device_fingerprint or self._build_device_fingerprint()
        self.session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/134.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        })

    def format_url(self, path: str) -> str:
        base = f'https://{self.host}/'
        return urljoin(base, path.lstrip('/'))

    def _build_device_fingerprint(self) -> str:
        return secrets.token_hex(16)

    def _build_headers(self, referer: str = '', xhr: bool = False) -> Dict[str, str]:
        headers = {
            'Referer': referer or self.format_url('auth/login'),
            'Origin': self.format_url('').rstrip('/'),
        }
        if xhr:
            headers.update({
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'X-Requested-With': 'XMLHttpRequest',
            })
        return headers

    def _get(self, path: str, referer: str = ''):
        url = path if path.startswith('http') else self.format_url(path)
        try:
            return self.session.get(
                url,
                timeout=self.timeout,
                verify=self.verify_tls,
                headers=self._build_headers(referer=referer),
            )
        except request_exceptions.SSLError as exc:
            raise RetryableError(f'TLS 证书验证失败：{exc}') from exc
        except request_exceptions.Timeout as exc:
            raise RetryableError(f'请求超时：{exc}') from exc
        except request_exceptions.RequestException as exc:
            raise RetryableError(f'网络请求失败：{exc}') from exc

    def _post(self, path: str, data: Dict[str, str], referer: str = ''):
        url = path if path.startswith('http') else self.format_url(path)
        try:
            return self.session.post(
                url,
                data=data,
                timeout=self.timeout,
                verify=self.verify_tls,
                headers=self._build_headers(referer=referer, xhr=True),
            )
        except request_exceptions.SSLError as exc:
            raise RetryableError(f'TLS 证书验证失败：{exc}') from exc
        except request_exceptions.Timeout as exc:
            raise RetryableError(f'请求超时：{exc}') from exc
        except request_exceptions.RequestException as exc:
            raise RetryableError(f'网络请求失败：{exc}') from exc

    def _parse_attrs(self, raw_attrs: str) -> Dict[str, str]:
        attrs = {}
        for name, _, value in self.ATTR_RE.findall(raw_attrs):
            attrs[name.lower()] = value
        return attrs

    def _extract_inputs(self, html: str) -> Dict[str, str]:
        data = {}
        for match in self.INPUT_TAG_RE.finditer(html):
            attrs = self._parse_attrs(match.group(1))
            name = attrs.get('name')
            if not name:
                continue
            input_type = attrs.get('type', '').lower()
            if input_type in {'submit', 'button', 'image', 'file'}:
                continue
            data[name] = attrs.get('value', '')
        return data

    def _extract_form_action(self, html: str, fallback_url: str) -> str:
        match = self.FORM_ACTION_RE.search(html)
        if not match:
            return fallback_url
        action = match.group(2).strip()
        if not action or action.lower().startswith('javascript:'):
            return fallback_url
        return urljoin(fallback_url, action)

    def _extract_altcha_url(self, html: str) -> str:
        match = self.ALTCHA_RE.search(html)
        if not match:
            return ''
        return urljoin(self.format_url(''), match.group(2).strip())

    def _hash_hex(self, algorithm: str, value: str) -> str:
        normalized = algorithm.lower().replace('-', '')
        return hashlib.new(normalized, value.encode('utf-8')).hexdigest()

    def _solve_altcha(self, challenge_url: str, referer: str) -> str:
        challenge = self._get(challenge_url, referer=referer).json()
        algorithm = challenge.get('algorithm', 'SHA-256')
        salt = challenge['salt']
        expected = challenge['challenge']
        maxnumber = int(challenge.get('maxnumber', 1000000))
        signature = challenge['signature']

        solved_number = None
        for number in range(maxnumber + 1):
            if self._hash_hex(algorithm, f'{salt}{number}') == expected:
                solved_number = number
                break

        if solved_number is None:
            raise RuntimeError('ALTCHA 验证求解失败')

        payload = {
            'algorithm': algorithm,
            'challenge': expected,
            'number': solved_number,
            'salt': salt,
            'signature': signature,
        }
        encoded = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        return base64.b64encode(encoded).decode('ascii')

    def _decode_json(self, response, action_name: str) -> dict:
        try:
            return response.json()
        except ValueError as exc:
            snippet = re.sub(r'\s+', ' ', response.text or '')[:160]
            raise RetryableError(
                f'{action_name} 未返回 JSON（HTTP {response.status_code}），可能是站点页面结构已变更或被风控拦截：{snippet}'
            ) from exc

    def _current_code(self, method: str = '') -> str:
        chosen_method = (method or '').strip().lower()
        if chosen_method == 'email':
            return self.code
        if self.secret:
            if pyotp is None:
                raise RuntimeError('缺少 pyotp 依赖，无法生成两步验证码')
            return pyotp.TOTP(self.secret).now()
        return self.code

    def _submit_form(self, page_response, html: str, overrides: Dict[str, str]) -> dict:
        form_data = self._extract_inputs(html)
        form_data.update(overrides)

        altcha_url = self._extract_altcha_url(html)
        if altcha_url:
            form_data['altcha'] = self._solve_altcha(altcha_url, referer=page_response.url)

        action_url = self._extract_form_action(html, fallback_url=page_response.url)
        response = self._post(action_url, form_data, referer=page_response.url)
        return self._decode_json(response, '表单提交')

    def _needs_device_2fa(self, result: dict) -> bool:
        return result.get('ret') == 2 and bool(result.get('need_device_2fa'))

    def _result_token(self, result: dict) -> str:
        token = str(result.get('token', '')).strip()
        if token:
            return token

        redirect = str(result.get('redirect', '')).strip()
        if not redirect:
            return ''

        parsed = urlparse(redirect)
        return parse_qs(parsed.query).get('token', [''])[0].strip()

    def _device_2fa_method(self, result: dict, form_data: Dict[str, str]) -> str:
        methods = result.get('methods') or {}
        if self.verify_method:
            if self.verify_method not in {'auto', 'ga', 'email'}:
                raise AuthError(f'不支持的 verify_method：{self.verify_method}，仅支持 ga、email 或留空自动选择')
            if self.verify_method == 'ga':
                if not methods.get('ga'):
                    raise AuthError('当前账号未启用验证器二步验证，无法使用 verify_method=ga')
                if not self.secret:
                    raise AuthError('verify_method=ga 需要同时提供 secret')
                return 'ga'
            if self.verify_method == 'email':
                if not methods.get('email'):
                    raise AuthError('当前账号未启用邮箱二步验证，无法使用 verify_method=email')
                if not self.code:
                    raise AuthError('verify_method=email 需要同时提供 code')
                return 'email'

        if self.secret and methods.get('ga'):
            return 'ga'
        if self.code and methods.get('email'):
            return 'email'

        default_method = str(form_data.get('method', 'email')).strip().lower()
        if default_method in methods and methods.get(default_method):
            return default_method
        if methods.get('ga'):
            return 'ga'
        if methods.get('email'):
            return 'email'
        return 'email'

    def _device_2fa(self, result: dict) -> dict:
        verify_url = result.get('redirect') or f'/auth/login/2fa?token={result.get("token", "")}'
        method = self._device_2fa_method(result, {})
        current_code = self._current_code(method)
        if not current_code:
            msg = result.get('msg') or '登录需要设备二次验证'
            missing = 'secret' if method == 'ga' else 'code'
            return {
                'ret': 0,
                'msg': f'{msg}，当前方式需要提供 {missing} 参数后再试',
            }

        token = self._result_token(result)
        if not token:
            raise RetryableError('站点返回的设备验证 token 缺失，无法继续完成二步验证')
        if method == 'ga':
            referer = verify_url if str(verify_url).startswith('http') else self.format_url(verify_url)
            payload = {
                'token': token,
                'code': current_code,
                'method': 'ga',
                'trust_device': '1' if self.trust_device else '0',
            }
            response = self._post('/auth/login/2fa/verify', payload, referer=referer)
            return self._decode_json(response, '表单提交')

        page = self._get(verify_url, referer=self.format_url('auth/login'))
        html = page.text
        if '验证会话已过期或无效' in html:
            return {'ret': 0, 'msg': '设备二次验证会话已过期或无效，请稍后重试'}

        form_data = self._extract_inputs(html)
        method = self._device_2fa_method(result, form_data)
        current_code = self._current_code(method)
        payload = {
            'token': form_data.get('token', token),
            'code': current_code,
            'method': method,
            'trust_device': '1' if self.trust_device else '0',
        }

        response = self._post('/auth/login/2fa/verify', payload, referer=page.url)
        return self._decode_json(response, '表单提交')

    def _get_user_page(self):
        response = self._get('user', referer=self.format_url('auth/login'))
        path = urlparse(response.url).path
        if '/auth/login' in path or 'id="login-form"' in response.text:
            raise RetryableError('当前会话未登录，站点返回了登录页')
        return response

    def login(self) -> dict:
        login_page = self._get('auth/login')
        overrides = {
            'email': self.email,
            'passwd': self.passwd,
            'device_fingerprint': self.device_fingerprint,
        }
        result = self._submit_form(login_page, login_page.text, overrides=overrides)
        if self._needs_device_2fa(result):
            return self._device_2fa(result)
        return result

    def check_in(self) -> dict:
        user_page = self._get_user_page()
        form_data = self._extract_inputs(user_page.text)
        payload = {}
        if 'csrf_token' in form_data:
            payload['csrf_token'] = form_data['csrf_token']
        response = self._post('user/checkin', payload, referer=user_page.url)
        return self._decode_json(response, '签到')

    def info(self) -> Tuple:
        html = self._get_user_page().text
        today_used = re.search(
            '<span class="traffic-info">今日已用</span>(.*?)<code class="card-tag tag-red">(.*?)</code>',
            html,
            re.S,
        )
        total_used = re.search(
            '<span class="traffic-info">过去已用</span>(.*?)<code class="card-tag tag-orange">(.*?)</code>',
            html,
            re.S,
        )
        rest = re.search(
            '<span class="traffic-info">剩余流量</span>(.*?)<code class="card-tag tag-green" id="remain">(.*?)</code>',
            html,
            re.S,
        )
        if today_used and total_used and rest:
            return today_used.group(2), total_used.group(2), rest.group(2)
        return ()

    def run(self):
        self.login()
        self.check_in()
        self.info()
