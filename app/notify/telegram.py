from app.errors import NotConfigured


class TelegramNotifier:
    def send(self, notification):
        raise NotConfigured(
            "Telegram: faltan TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, vínculo de dueño y transporte en notify/telegram.py"
        )

    def parse_action(self, body, headers):
        raise NotConfigured(
            "Telegram webhook: falta verificar secret_token y normalizar callback/chat_id antes de shared owner_action"
        )
