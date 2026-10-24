from app.errors import NotConfigured


class WaSenderChannel:
    def _missing(self):
        # VERIFICAR-DOCS: parser, signatures, owner echo and send semantics need real fixtures.
        raise NotConfigured(
            "WaSender: faltan cuenta, token cifrado, secreto webhook y contrato verificado con fixtures reales; implementar channels/wasender"
        )

    def parse_inbound(self, request):
        self._missing()

    def send_text(self, account, to, text):
        self._missing()

    def send_typing(self, account, to):
        self._missing()

    def session_status(self, account):
        self._missing()
