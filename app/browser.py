"""Reuse an authenticated local browser session; never persist its cookies."""

import json
import subprocess
from urllib.parse import urlparse


class BrowserError(RuntimeError):
    pass


def _call(*args, timeout=30):
    try:
        process = subprocess.run(
            ['agent-browser-cli', *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise BrowserError('浏览器模式需要 agent-browser-cli 和已连接的浏览器扩展') from exc
    except subprocess.TimeoutExpired as exc:
        raise BrowserError('浏览器操作超时，请检查浏览器扩展连接和验证页面') from exc
    try:
        result = json.loads(process.stdout)
    except ValueError as exc:
        raise BrowserError('浏览器工具未返回有效 JSON，请检查 agent-browser-cli 连接') from exc
    if process.returncode or not result.get('ok'):
        # Tool responses can contain cookies/tokens; do not include raw output.
        raise BrowserError('浏览器操作失败，请检查 agent-browser-cli 连接和验证页面')
    return result['result']


def load_session(url):
    """Read only cookies for the requested site from an existing account tab."""
    origin = urlparse(url)
    tabs = _call('tabs')['metadata']['tabs']
    matches = [tab for tab in tabs if (
        urlparse(tab.get('url', '')).scheme == origin.scheme
        and urlparse(tab.get('url', '')).netloc == origin.netloc
        and (urlparse(tab.get('url', '')).path == '/user'
             or urlparse(tab.get('url', '')).path.startswith('/user/'))
    )]
    if not matches:
        raise BrowserError(f'请先在已连接的浏览器中登录并打开 {url}')
    if len({tab['profile_id'] for tab in matches}) > 1:
        raise BrowserError('多个浏览器配置中均打开了本站，请只保留需要签到的账号页面')
    tab = matches[0]
    data = _call(
        'exec', '--tab', str(tab['tab_id']),
        '--browser', tab['browser_id'], '--profile', tab['profile_id'],
        'return {url: location.href, user_agent: navigator.userAgent, '
        'login_form: !!document.querySelector("#login-form")}',
    )['js_return']
    current = urlparse(data['url'])
    if (current.scheme, current.netloc) != (origin.scheme, origin.netloc) or (
        current.path != '/user' and not current.path.startswith('/user/')
    ) or data['login_form']:
        raise BrowserError('浏览器登录已失效，请重新登录后再执行签到')
    cookies = _call('exec', json.dumps({
        'cmd': 'cdp', 'tabId': int(tab['tab_id']),
        'sessionKey': tab['session_key'],
        'method': 'Network.getCookies', 'params': {'urls': [url]},
    }))['js_return']['cookies']
    if not cookies:
        raise BrowserError('浏览器没有可用的站点 Cookie，请重新登录')
    return data['user_agent'], cookies
