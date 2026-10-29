"""Complete the site's Cap widget in a fresh, non-persistent Chromium session."""

import re
import time
from urllib.parse import urlparse

from app import log


def _cap_diagnostic(text):
    """Keep only verified public codes/templates, never arbitrary console text."""
    if '[instr_blocked]' in text:
        match = re.search(r'\[instr_blocked\] Instrumentation blocked \(([^)]+)\)', text)
        # The site's widget uses this public fallback. Unknown reasons may contain
        # identifiers or credentials, so do not guess that a snake_case word is safe.
        reason = 'automated_browser' if match and match[1] == 'automated_browser' else 'redacted_unknown'
        return 'instr_blocked', reason
    if '[instr_timeout]' in text:
        return 'instr_timeout', 'instrumentation_timeout'
    return None


class HeadlessError(RuntimeError):
    pass


def prepare_login(url: str, verify_tls: bool = True, timeout: float = 90):
    started = time.monotonic()
    stage = 'import_playwright'

    def diagnostic(event, details=''):
        log.info(f'Cap diagnostic stage={stage} event={event} '
                 f'elapsed_s={time.monotonic() - started:.1f}'
                 + (f' {details}' if details else ''))

    diagnostic('start')
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as exc:
        diagnostic('failed', 'reason=missing_playwright')
        raise HeadlessError('缺少 Playwright，请先安装 requirements.txt 和 Chromium') from exc

    try:
        with sync_playwright() as playwright:
            stage = 'launch_browser'
            diagnostic('start')
            browser = playwright.chromium.launch(channel='chromium', headless=True)
            try:
                version = str(browser.version)
                version = version if re.fullmatch(r'\d{1,4}(?:\.\d{1,5}){3}', version) else 'unknown'
                diagnostic('ready', f'chromium_version={version} headless=true')
                stage = 'create_context'
                context = browser.new_context(ignore_https_errors=not verify_tls)
                page = context.new_page()
                blocked = []
                seen = set()

                def on_console(message):
                    result = _cap_diagnostic(message.text)
                    if result is None or result in seen:
                        return
                    seen.add(result)
                    code, reason = result
                    diagnostic('widget_error', f'code={code} reason={reason}')
                    if code == 'instr_blocked':
                        blocked.append(reason)

                page.on('console', on_console)
                stage = 'load_login_page'
                diagnostic('start')
                response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
                diagnostic('response', f'http_status={response.status if response else "none"}')
                if response is None or response.status >= 400:
                    diagnostic('failed', 'reason=login_page_http_error')
                    raise HeadlessError('无头浏览器无法加载登录页')
                expected = urlparse(url)
                stage = 'wait_cap'
                diagnostic('start')
                last_presence = None
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    current = urlparse(page.url)
                    if (current.scheme, current.netloc, current.path) != (
                        expected.scheme, expected.netloc, expected.path,
                    ):
                        diagnostic('failed', 'reason=unexpected_redirect')
                        raise HeadlessError('登录页发生非预期跳转，停止认证')
                    if blocked:
                        diagnostic('failed', f'code=instr_blocked reason={blocked[0]}')
                        raise HeadlessError(f'Cap 拒绝当前浏览器环境（code=instr_blocked, reason={blocked[0]}）')
                    # wait_for_function/locator inject helper globals which the
                    # site's instrumentation rejects. Query without those helpers;
                    # the original site widget performs all verification itself.
                    data = page.evaluate('''() => ({
                        cap_token: document.querySelector('cap-widget')?.tokenValue || '',
                        csrf_token: document.querySelector('input[name=csrf_token]')?.value || '',
                        device_fingerprint: typeof deviceFingerprint !== 'undefined' ? deviceFingerprint : '',
                        user_agent: navigator.userAgent
                    })''')
                    fields = ('cap_token', 'csrf_token', 'device_fingerprint')
                    presence = tuple(bool(data.get(key)) for key in fields)
                    if presence != last_presence:
                        diagnostic('fields', ' '.join(
                            f'{key}_present={str(value).lower()}'
                            for key, value in zip(fields, presence)
                        ))
                        last_presence = presence
                    if all(data.get(key) for key in (
                        'cap_token', 'csrf_token', 'device_fingerprint',
                    )):
                        stage = 'collect_session'
                        cookies = context.cookies([url])
                        diagnostic('ready')
                        return data, cookies
                    time.sleep(0.5)
                diagnostic('failed', 'reason=cap_wait_timeout')
                raise HeadlessError('Cap 验证超时，请检查站点验证服务是否可达')
            finally:
                browser.close()
    except Error as exc:
        diagnostic('failed', 'reason=playwright_error')
        # Do not emit browser dumps, session cookies or verification tokens.
        raise HeadlessError(
            '无头浏览器启动或页面请求失败；请确认网络正常，并已执行 '
            'python -m playwright install --with-deps --no-shell chromium'
        ) from exc
