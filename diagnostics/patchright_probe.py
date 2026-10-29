"""One fresh, credential-free Cap probe; never print or export tokens/cookies."""

import json
import re
import tempfile
import time
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlparse

from patchright.sync_api import Error, sync_playwright


LOGIN_URL = 'https://cordcloud.biz/auth/login'
CAP_HOST = 'cj.skssugetcc.org'
TIMEOUT_SECONDS = 45


def main():
    started = time.monotonic()
    stage = 'launch_browser'

    def emit(event, **fields):
        print(json.dumps({'event': event, 'stage': stage,
                          'elapsed_s': round(time.monotonic() - started, 1),
                          **fields}, sort_keys=True), flush=True)

    emit('probe_start', patchright_version=version('patchright'), headless=False,
         credentials_used=False, persistent_profile_reused=False)
    # Keep the temporary profile inside the project/run; remove it on exit.
    profile_root = Path(__file__).resolve().parents[1] / '.work'
    profile_root.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='cap-probe-', dir=profile_root) as profile:
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=profile, channel='chrome', headless=False,
                    no_viewport=True,
                )
                try:
                    browser_version = context.browser.version if context.browser else ''
                    browser_version = (browser_version if re.fullmatch(
                        r'\d{1,4}(?:\.\d{1,5}){3}', browser_version) else 'unknown')
                    emit('browser_ready', browser_version=browser_version)
                    page = context.pages[0] if context.pages else context.new_page()
                    http_events = set()

                    def on_response(response):
                        parsed = urlparse(response.url)
                        endpoint = parsed.path.rstrip('/').rsplit('/', 1)[-1]
                        if parsed.hostname != CAP_HOST or endpoint not in {'challenge', 'redeem'}:
                            return
                        key = (endpoint, response.status)
                        if key not in http_events:
                            http_events.add(key)
                            emit('cap_http', endpoint=endpoint, status=response.status)

                    # Patchright disables console events. Only log public HTTP
                    # statuses, never response bodies, headers, URLs or exceptions.
                    page.on('response', on_response)
                    stage = 'load_login_page'
                    response = page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=45000)
                    status = response.status if response else None
                    emit('login_http', status=status)
                    if status is None or status >= 400:
                        emit('probe_failed', reason='login_http_error')
                        return 1
                    stage = 'wait_cap'
                    deadline = time.monotonic() + TIMEOUT_SECONDS
                    previous = None
                    while time.monotonic() < deadline:
                        parsed = urlparse(page.url)
                        if (parsed.scheme, parsed.netloc, parsed.path) != (
                                'https', 'cordcloud.biz', '/auth/login'):
                            emit('probe_failed', reason='unexpected_redirect')
                            return 1
                        # Custom element properties and page globals live in the
                        # main world. Return only booleans across the CDP boundary.
                        state = page.evaluate('''() => {
                            const token = document.querySelector('cap-widget')?.tokenValue;
                            return {
                                cap_token_present: typeof token === 'string' && token.length > 0,
                                csrf_token_present: !!document.querySelector('input[name=csrf_token]')?.value,
                                device_fingerprint_present: typeof deviceFingerprint !== 'undefined' && !!deviceFingerprint
                            };
                        }''', isolated_context=False)
                        state = {key: bool(state.get(key)) for key in (
                            'cap_token_present', 'csrf_token_present', 'device_fingerprint_present')}
                        if state != previous:
                            emit('field_presence', **state)
                            previous = state
                        if state['cap_token_present']:
                            emit('probe_success', **state, credentials_used=False)
                            return 0
                        # A rejection is evidence, not a reason to request a new challenge.
                        if any(status >= 400 for _, status in http_events):
                            emit('probe_failed', reason='cap_http_rejected')
                            return 1
                        page.wait_for_timeout(500)
                    emit('probe_failed', reason='cap_timeout')
                    return 1
                finally:
                    context.close()
    except Error:
        emit('probe_failed', reason='browser_error')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
