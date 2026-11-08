from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from app.currencies import normalize_currency_code


class TenantCreate(BaseModel):
    name: str
    slug: str
    default_currency: str = "PKR"

    @field_validator("default_currency")
    @classmethod
    def validate_default_currency(cls, value: str) -> str:
        return normalize_currency_code(value)


class TenantRegistration(BaseModel):
    name: str
    slug: str
    admin_email: EmailStr
    admin_password: str = Field(min_length=12, max_length=1024)
    default_currency: str = "PKR"

    @field_validator("default_currency")
    @classmethod
    def validate_default_currency(cls, value: str) -> str:
        return normalize_currency_code(value)


class TenantResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str
    slug: str
    default_currency: str
    created_at: datetime


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=1024)
    tenant_id: str | None = None


class UserResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    email: EmailStr
    role: str
    is_active: bool
    tenant_id: str | None


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    email: EmailStr | None = None
    role: str | None = None
    tenant_id: str | None = None


class ProductCreate(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=5000)
    price: Decimal = Field(gt=0, max_digits=15, decimal_places=4)
    currency: str | None = None
    stock: int = Field(default=0, ge=0)
    is_available: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Product name must not be blank")
        return value

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str | None) -> str | None:
        return normalize_currency_code(value) if value is not None else None


class ProductUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=5000)
    price: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=15,
        decimal_places=4,
    )
    currency: str | None = None
    stock: int | None = Field(default=None, ge=0)
    is_available: bool | None = None
    expected_version: int = Field(ge=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Product name must not be blank")
        return value

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str | None) -> str | None:
        return normalize_currency_code(value) if value is not None else None

    @model_validator(mode="after")
    def validate_partial_update(self) -> "ProductUpdate":
        mutable_fields = self.model_fields_set - {"expected_version"}
        if not mutable_fields:
            raise ValueError("At least one product field must be provided")
        for field_name in ("name", "price", "currency", "stock", "is_available"):
            if (
                field_name in self.model_fields_set
                and getattr(self, field_name) is None
            ):
                raise ValueError(f"{field_name} cannot be null")
        return self


class ProductResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    name: str
    description: str | None
    price: Decimal
    currency: str
    stock: int
    is_available: bool
    tenant_id: str
    version: int

    @field_serializer("price")
    def serialize_price(self, value: Decimal) -> str:
        formatted = format(value, "f")
        return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


class CartItemAdd(BaseModel):
    model_config = {"extra": "forbid"}

    product_id: int = Field(gt=0, strict=True)
    quantity: int = Field(ge=1, le=1000, strict=True)


class CartItemUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    quantity: int = Field(ge=1, le=1000, strict=True)


class CartItemResponse(BaseModel):
    product_id: int
    product_name: str
    quantity: int
    unit_price: Decimal = Field(max_digits=15, decimal_places=4)
    currency: str
    is_available: bool
    line_total: Decimal

    @field_serializer("unit_price", "line_total")
    def serialize_amount(self, value: Decimal) -> str:
        formatted = format(value, "f")
        return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


class CartResponse(BaseModel):
    id: int | None
    updated_at: datetime | None
    items: list[CartItemResponse]


class OrderItemResponse(BaseModel):
    line_number: int
    product_id: int | None
    product_name: str
    unit_price: Decimal
    currency: str
    quantity: int
    line_total: Decimal

    @field_serializer("unit_price", "line_total")
    def serialize_amount(self, value: Decimal) -> str:
        formatted = format(value, "f")
        return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


class OrderSummary(BaseModel):
    id: int
    status: str
    currency: str
    subtotal: Decimal
    total: Decimal
    created_at: datetime
    expires_at: datetime

    @field_serializer("subtotal", "total")
    def serialize_amount(self, value: Decimal) -> str:
        formatted = format(value, "f")
        return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


class OrderResponse(OrderSummary):
    items: list[OrderItemResponse]


OrderFulfillmentStatus = Literal["confirmed", "processing", "completed"]


class OrderFulfillmentUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    fulfillment_status: OrderFulfillmentStatus


class PaymentSummary(BaseModel):
    id: int
    order_id: int
    status: str
    amount: Decimal
    currency: str
    created_at: datetime
    updated_at: datetime

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal) -> str:
        formatted = format(value, "f")
        return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted
