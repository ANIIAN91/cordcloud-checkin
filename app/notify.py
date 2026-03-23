from typing import Dict

import requests


class NotifyError(RuntimeError):
    pass


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str = '',
        chat_id: str = '',
    ):
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.timeout = 15

    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _api_url(self, method: str) -> str:
        return f'https://api.telegram.org/bot{self.bot_token}/{method}'

    def send(self, text: str) -> bool:
        message = text.strip()
        if not message or not self.enabled():
            return False

        payload: Dict[str, str] = {
            'chat_id': self.chat_id,
            'text': message,
        }

        try:
            response = requests.post(
                self._api_url('sendMessage'),
                data=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise NotifyError(f'Telegram 网络请求失败：{exc}') from exc

        try:
            result = response.json()
        except ValueError as exc:
            snippet = (response.text or '').strip()[:160]
            raise NotifyError(
                f'Telegram 未返回 JSON（HTTP {response.status_code}）：{snippet}'
            ) from exc

        if response.status_code >= 400 or not result.get('ok'):
            description = result.get('description') or f'HTTP {response.status_code}'
            raise NotifyError(f'Telegram 推送失败：{description}')

        return True
