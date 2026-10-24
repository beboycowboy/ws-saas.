from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, Field

from app.config import StrictModel


class InboundPayload(StrictModel):
    provider_message_id: str = Field(min_length=1, max_length=200)
    contact_id: str = Field(min_length=1, max_length=200)
    phone_e164: str | None = Field(default=None, pattern=r"^\+[1-9]\d{6,14}$")
    text: str = Field(default="", max_length=4000)
    timestamp: AwareDatetime
    is_from_me: bool = False
    is_group: bool = False
    kind: Literal["text", "unsupported"] = "text"


@dataclass(frozen=True)
class InboundEvent:
    channel_account_id: UUID
    payload: InboundPayload


@dataclass(frozen=True)
class InboundRequest:
    account: dict
    body: bytes
    headers: dict


@dataclass(frozen=True)
class SendResult:
    provider_message_id: str
    delivered_at: datetime


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0


class Channel(Protocol):
    def parse_inbound(self, request: InboundRequest) -> InboundEvent | None: ...
    def send_text(self, account: dict, to: str, text: str) -> SendResult: ...
    def send_typing(self, account: dict, to: str) -> None: ...
    def session_status(self, account: dict) -> Literal["Connected", "Disconnected", "Unknown"]: ...


class LLMProvider(Protocol):
    def generate(
        self, messages: list[dict], tools: list[dict], max_tokens: int, temperature: float
    ) -> LLMResponse: ...


class Notifier(Protocol):
    def send(self, notification: dict) -> None: ...


class SecretStore(Protocol):
    def encrypt(self, value: str) -> str: ...
    def decrypt(self, value: str) -> str: ...


class BackupTarget(Protocol):
    def put(self, name: str, data: bytes) -> str: ...
    def get(self, name: str) -> bytes: ...
