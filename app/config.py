from datetime import date, time
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Features(StrictModel):
    appointments: bool = True
    orders: bool = False


class OwnerConfig(StrictModel):
    email: str
    telegram_link_code: str = "autogenerado"


class TenantConfig(StrictModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)
    name: str = Field(min_length=1, max_length=120)
    timezone: str = "America/Mexico_City"
    language: Literal["es-MX"] = "es-MX"
    tone: str = "amable, breve, usa tú"
    address: str = ""
    owner: OwnerConfig
    handoff_hours: int = Field(default=4, ge=1, le=48)
    features: Features = Field(default_factory=Features)

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        ZoneInfo(value)
        return value


def cents(value: Decimal) -> int:
    result = value * 100
    if result != result.to_integral_value():
        raise ValueError("El precio admite como máximo dos decimales")
    return int(result)


class ServiceConfig(StrictModel):
    code: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    minutes: int = Field(ge=5, le=480)
    buffer_minutes: int = Field(default=0, ge=0, le=120)
    price_mxn: Decimal = Field(ge=0, le=100000)

    @field_validator("price_mxn")
    @classmethod
    def valid_price(cls, value):
        cents(value)
        return value


Hours = dict[str, list[tuple[str, str]]]


def validate_hours(value: Hours):
    for day, ranges in value.items():
        if day not in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
            raise ValueError("Día inválido")
        previous_end = None
        for start, end in sorted(ranges):
            a, b = time.fromisoformat(start), time.fromisoformat(end)
            if a >= b or (previous_end and a < previous_end):
                raise ValueError("Horario invertido, nocturno o solapado; dividir por día")
            previous_end = b
    return value


class AppointmentConfig(StrictModel):
    slot_step_minutes: int = Field(default=15, ge=5, le=120)
    min_notice_minutes: int = Field(default=60, ge=0, le=10080)
    max_days_ahead: int = Field(default=30, ge=1, le=90)
    weekly_hours: Hours = Field(default_factory=dict)
    closed_dates: list[date] = Field(default_factory=list)
    date_overrides: dict[date, list[tuple[str, str]]] = Field(default_factory=dict)
    services: list[ServiceConfig] = Field(default_factory=list, max_length=150)

    @field_validator("weekly_hours")
    @classmethod
    def valid_hours(cls, value):
        return validate_hours(value)

    @field_validator("date_overrides")
    @classmethod
    def valid_overrides(cls, value):
        for ranges in value.values():
            validate_hours({"mon": ranges})
        return value


class ItemConfig(StrictModel):
    code: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    price_mxn: Decimal = Field(ge=0, le=100000)
    available: bool = True

    @field_validator("price_mxn")
    @classmethod
    def valid_price(cls, value):
        cents(value)
        return value


class OrderConfig(StrictModel):
    accepts: list[Literal["pickup", "delivery"]] = ["pickup"]
    delivery_note: str = ""
    delivery_fee_mxn: Decimal = Field(default=Decimal(0), ge=0, le=10000)
    min_order_mxn: Decimal = Field(default=Decimal(0), ge=0, le=100000)
    hours: Hours = Field(default_factory=dict)
    items: list[ItemConfig] = Field(default_factory=list, max_length=150)

    @field_validator("delivery_fee_mxn", "min_order_mxn")
    @classmethod
    def valid_price(cls, value):
        cents(value)
        return value

    @field_validator("hours")
    @classmethod
    def valid_hours(cls, value):
        return validate_hours(value)


class FAQ(StrictModel):
    q: str
    a: str


class Policies(StrictModel):
    no_health_data: Literal[True] = True
    cancel_policy: str = "Cancela con 2 horas de anticipación."
    cancellation_notice_minutes: int = Field(default=120, ge=0, le=10080)


class BusinessConfig(StrictModel):
    tenant: TenantConfig
    appointments: AppointmentConfig = Field(default_factory=AppointmentConfig)
    orders: OrderConfig = Field(default_factory=OrderConfig)
    faq: list[FAQ] = Field(default_factory=list, max_length=100)
    policies: Policies = Field(default_factory=Policies)

    @model_validator(mode="after")
    def unique_catalogs(self):
        for items in [self.appointments.services, self.orders.items]:
            if len({i.code for i in items}) != len(items):
                raise ValueError("Los códigos de catálogo deben ser únicos")
        return self
