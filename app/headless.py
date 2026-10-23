"""Complete the site's Cap widget in a fresh, non-persistent Chromium session."""

import time
from urllib.parse import urlparse


class HeadlessError(RuntimeError):
    pass


def prepare_login(url: str, verify_tls: bool = True, timeout: float = 90):
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as exc:
        raise HeadlessError('缺少 Playwright，请先安装 requirements.txt 和 Chromium') from exc

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chromium', headless=True)
            try:
                context = browser.new_context(ignore_https_errors=not verify_tls)
                page = context.new_page()
                blocked = []
                page.on('console', lambda message: blocked.append(True)
                        if '[instr_blocked]' in message.text else None)
                response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
                if response is None or response.status >= 400:
                    raise HeadlessError('无头浏览器无法加载登录页')
                expected = urlparse(url)
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    current = urlparse(page.url)
                    if (current.scheme, current.netloc, current.path) != (
                        expected.scheme, expected.netloc, expected.path,
                    ):
                        raise HeadlessError('登录页发生非预期跳转，停止认证')
                    if blocked:
                        raise HeadlessError('Cap 拒绝当前浏览器环境（automated_browser）')
                    # wait_for_function/locator inject helper globals which the
                    # site's instrumentation rejects. Query without those helpers;
                    # the original site widget performs all verification itself.
                    data = page.evaluate('''() => ({
                        cap_token: document.querySelector('cap-widget')?.tokenValue || '',
                        csrf_token: document.querySelector('input[name=csrf_token]')?.value || '',
                        device_fingerprint: typeof deviceFingerprint !== 'undefined' ? deviceFingerprint : '',
                        user_agent: navigator.userAgent
                    })''')
                    if all(data.get(key) for key in (
                        'cap_token', 'csrf_token', 'device_fingerprint',
                    )):
                        return data, context.cookies([url])
                    time.sleep(0.5)
                raise HeadlessError('Cap 验证超时，请检查站点验证服务是否可达')
            finally:
                browser.close()
    except Error as exc:
        # Do not emit browser dumps, session cookies or verification tokens.
        raise HeadlessError(
            '无头浏览器启动或页面请求失败；请确认网络正常，并已执行 '
            'python -m playwright install --with-deps --no-shell chromium'
        ) from exc
