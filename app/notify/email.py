from app.errors import NotConfigured


class EmailNotifier:
    def send(self, notification):
        raise NotConfigured(
            "Email: faltan SMTP_URL o EMAIL_API_KEY, EMAIL_FROM, SPF/DKIM y transporte en notify/email.py"
        )
