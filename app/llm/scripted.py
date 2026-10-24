import re
import shlex

from app.ports import LLMResponse

HELP = "Puedo ayudarte con citas o pedidos. Escribe horarios, menú o humano."


class ScriptedLLM:
    """Deterministic local script. No external provider and no business writes here."""

    def __init__(self):
        self.failure_mode = None

    def generate(self, messages, tools, max_tokens, temperature):
        if self.failure_mode == "timeout":
            raise TimeoutError("Simulated LLM timeout")
        if self.failure_mode == "error":
            raise ConnectionError("Simulated LLM error")
        if self.failure_mode == "invalid":
            return LLMResponse()
        text = next(m["content"] for m in reversed(messages) if m["role"] == "user").strip()
        lower = text.casefold()
        if any(word in lower for word in ["ignora", "descuento", "otro cliente", "system prompt"]):
            return LLMResponse(
                text="Solo puedo ayudarte con tus citas y pedidos usando los precios del negocio."
            )
        try:
            parts = shlex.split(text)
            command = parts[0].casefold() if parts else ""
            name, args = None, {}
            if command == "horarios" and len(parts) == 3:
                name, args = (
                    "consultar_disponibilidad",
                    {"service_code": parts[1], "date_from": parts[2], "date_to": parts[2]},
                )
            elif command in {"agendar", "cita"} and len(parts) >= 3:
                name, args = (
                    "preparar_cita",
                    {
                        "service_code": parts[1],
                        "starts_at_local": parts[2],
                        "customer_name": " ".join(parts[3:]) or "Cliente",
                    },
                )
            elif lower in {"mis citas", "consultar citas"}:
                name = "consultar_mis_citas"
            elif command == "cancelar" and len(parts) == 3 and parts[1] == "cita":
                name, args = "cancelar_cita", {"appointment_id": parts[2]}
            elif lower in {"menú", "menu", "ver menú", "ver menu"}:
                name = "ver_menu"
            elif command == "agregar" and len(parts) == 3:
                name, args = "agregar_al_pedido", {"item_code": parts[1], "qty": int(parts[2])}
            elif command == "quitar" and len(parts) in {2, 3}:
                name, args = (
                    "quitar_del_pedido",
                    {"item_code": parts[1], "qty": int(parts[2]) if len(parts) == 3 else None},
                )
            elif lower in {"pedido", "ver pedido"}:
                name = "ver_pedido"
            elif command == "pedido" and len(parts) >= 2:
                name, args = "preparar_pedido", {"fulfillment": parts[1], "address_note": " ".join(parts[2:])}
            elif lower == "cancelar pedido":
                name = "cancelar_pedido"
            elif re.search(r"\b(humano|persona|asesor)\b", lower):
                name, args = "pasar_a_humano", {"reason": "Solicitud del cliente"}
            if name:
                return LLMResponse(
                    tool_calls=[{"name": name, "arguments": args}], tokens_in=40, tokens_out=20
                )
        except (ValueError, IndexError):
            return LLMResponse(text="Revisa el formato del comando. Escribe ayuda para ver ejemplos.")
        config = messages[0].get("business", {})
        if lower in {"dirección", "direccion"}:
            return LLMResponse(text=config.get("address", "Consulta la dirección con el negocio."))
        for entry in config.get("faq", []):
            if lower == entry["q"].casefold():
                return LLMResponse(text=entry["a"])
        return LLMResponse(text=HELP, tokens_in=30, tokens_out=20)
