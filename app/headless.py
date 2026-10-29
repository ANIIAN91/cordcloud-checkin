"""Prepare a Cap login with Patchright/Chrome and a disposable browser profile."""

import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from app import log


class HeadlessError(RuntimeError):
    pass


def _cap_reason(payload):
    """Only public error codes may cross the diagnostic boundary."""
    allowed = {'automated_browser', 'instr_blocked', 'instr_timeout',
               'instr_automated_browser', 'instr_probe_missing'}
    if isinstance(payload, dict):
        for key in ('code', 'reason', 'error'):
            value = payload.get(key)
            if isinstance(value, str) and value in allowed:
                return value
    return 'redacted_unknown'


def prepare_login(url: str, verify_tls: bool = True, timeout: float = 90):
    started = time.monotonic()
    stage = 'import_patchright'

    def diagnostic(event, details=''):
        log.info(f'Cap diagnostic stage={stage} event={event} '
                 f'elapsed_s={time.monotonic() - started:.1f}'
                 + (f' {details}' if details else ''))

    diagnostic('start')
    try:
        from patchright.sync_api import Error, sync_playwright
    except ImportError as exc:
        diagnostic('failed', 'reason=missing_patchright')
        raise HeadlessError('缺少 Patchright，请先安装 requirements.txt 和 Google Chrome') from exc

    profile_root = Path(__file__).resolve().parents[1] / '.work'
    profile_root.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='cap-login-', dir=profile_root) as profile:
            with sync_playwright() as playwright:
                stage = 'launch_browser'
                diagnostic('start')
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=profile, channel='chrome', headless=False,
                    no_viewport=True, ignore_https_errors=not verify_tls,
                )
                try:
                    version = context.browser.version if context.browser else ''
                    version = version if re.fullmatch(r'\d{1,4}(?:\.\d{1,5}){3}', version) else 'unknown'
                    diagnostic('ready', f'chrome_version={version} headless=false profile_reused=false')
                    page = context.pages[0] if context.pages else context.new_page()
                    pending = []
                    seen = set()

                    def on_response(response):
                        endpoint = urlparse(response.url).path.rstrip('/').rsplit('/', 1)[-1]
                        if endpoint in {'challenge', 'redeem'} and len(pending) < 20:
                            pending.append(response)

                    # Console events are unavailable. Match responses to the
                    # widget's actual API URL before logging or interpreting them.
                    page.on('response', on_response)
                    stage = 'load_login_page'
                    diagnostic('start')
                    response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
                    diagnostic('response', f'http_status={response.status if response else "none"}')
                    if response is None or response.status >= 400:
                        diagnostic('failed', 'reason=login_page_http_error')
                        raise HeadlessError('浏览器无法加载登录页')
                    expected = urlparse(url)
                    stage = 'wait_cap'
                    diagnostic('start')
                    last_presence = None
                    deadline = time.monotonic() + timeout
                    fields = ('cap_token', 'csrf_token', 'device_fingerprint', 'user_agent')
                    while time.monotonic() < deadline:
                        current = urlparse(page.url)
                        if (current.scheme, current.netloc, current.path) != (
                                expected.scheme, expected.netloc, expected.path):
                            diagnostic('failed', 'reason=unexpected_redirect')
                            raise HeadlessError('登录页发生非预期跳转，停止认证')
                        # Main-world properties; secrets stay in memory.
                        data = page.evaluate('''() => ({
                            cap_token: document.querySelector('cap-widget')?.tokenValue || '',
                            csrf_token: document.querySelector('input[name=csrf_token]')?.value || '',
                            device_fingerprint: typeof deviceFingerprint !== 'undefined' ? deviceFingerprint : '',
                            user_agent: navigator.userAgent,
                            cap_endpoint: document.querySelector('cap-widget')?.getAttribute('data-cap-api-endpoint') || ''
                        })''', isolated_context=False)
                        api = data.pop('cap_endpoint', '')
                        endpoints = {}
                        if isinstance(api, str) and api:
                            for name in ('challenge', 'redeem'):
                                target = urlparse(urljoin(urljoin(url, api).rstrip('/') + '/', name))
                                endpoints[(target.scheme, target.netloc, target.path)] = name
                        responses = pending[:]
                        pending.clear()
                        for response in responses:
                            target = urlparse(response.url)
                            endpoint = endpoints.get((target.scheme, target.netloc, target.path))
                            if endpoint is None:
                                continue
                            key = (endpoint, response.status)
                            if key not in seen:
                                diagnostic('cap_http', f'endpoint={endpoint} http_status={response.status}')
                                seen.add(key)
                            if response.status >= 400:
                                try:
                                    reason = _cap_reason(response.json())
                                except (ValueError, Error):
                                    reason = 'redacted_unknown'
                                diagnostic('failed', f'reason=cap_http_rejected code={reason}')
                                raise HeadlessError(
                                    f'Cap 验证接口拒绝请求（HTTP {response.status}, code={reason}）')
                        presence = tuple(isinstance(data.get(key), str) and bool(data[key]) for key in fields)
                        if presence != last_presence:
                            diagnostic('fields', ' '.join(
                                f'{key}_present={str(value).lower()}'
                                for key, value in zip(fields, presence)))
                            last_presence = presence
                        if all(presence):
                            stage = 'collect_session'
                            cookies = context.cookies([url])
                            diagnostic('ready')
                            return {key: data[key] for key in fields}, cookies
                        page.wait_for_timeout(500)
                    diagnostic('failed', 'reason=cap_wait_timeout')
                    raise HeadlessError('Cap 验证超时，请检查站点验证服务是否可达')
                finally:
                    context.close()
    except Error as exc:
        diagnostic('failed', 'reason=browser_error')
        raise HeadlessError(
            '浏览器启动或页面请求失败；请确认已安装 Google Chrome，'
            'Linux 无桌面环境需通过 xvfb-run -a 运行'
        ) from exc
