from app.errors import NotConfigured


class OpenAILLM:
    def generate(self, messages, tools, max_tokens, temperature):
        raise NotConfigured(
            "LLM: faltan LLM_API_KEY, LLM_MODEL, política de datos y transporte en llm/openai.py"
        )
