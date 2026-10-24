from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    def __init__(self, value: datetime):
        self._lock = Lock()
        self.set(value)

    def now(self) -> datetime:
        with self._lock:
            return self._value

    def set(self, value: datetime):
        if value.tzinfo is None:
            raise ValueError("Clock necesita fecha con zona horaria")
        with self._lock:
            self._value = value.astimezone(UTC)

    def advance(self, seconds: float):
        with self._lock:
            self._value += timedelta(seconds=seconds)
