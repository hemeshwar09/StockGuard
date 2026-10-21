import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field


class ProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    price_cents: int = Field(ge=0)
    stock: int = Field(ge=0, default=0)


class RestockRequest(BaseModel):
    quantity: int = Field(gt=0, le=1_000_000)


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    name: str
    price_cents: int
    stock: int
    reserved: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def available(self) -> int:
        return self.stock - self.reserved


class AuditOut(BaseModel):
    product_id: int
    stock: int
    reserved: int
    available: int
    active_reservation_qty: int
    consistent: bool  # reserved == sum(ACTIVE reservations) and 0 <= reserved <= stock
    oversold: bool  # more units promised to ACTIVE reservations than exist


class ReservationCreate(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=100)


class ReservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: int
    quantity: int
    status: str
    expires_at: datetime
    created_at: datetime


class OrderCreate(BaseModel):
    reservation_id: uuid.UUID


class PayRequest(BaseModel):
    # Stand-in for a payment gateway result. In production this comes from the
    # gateway (synchronous response or webhook), never from the client.
    outcome: Literal["success", "failure"] = "success"


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    reservation_id: uuid.UUID
    product_id: int
    quantity: int
    unit_price_cents: int
    total_cents: int
    status: str
    created_at: datetime
    updated_at: datetime
