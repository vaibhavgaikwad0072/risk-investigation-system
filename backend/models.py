from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, validates

from database import Base

SUPPORTED_ACCOUNT_CURRENCIES = frozenset({"INR", "USD", "EUR"})


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint(
            "length(country) = 2 AND country = upper(country)",
            name="ck_customers_country_format",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    customer_ref: Mapped[str] = mapped_column(
        String(30),
        unique=True,
        nullable=False,
    )

    full_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    email: Mapped[str | None] = mapped_column(
        String(254),
        unique=True,
        nullable=True,
    )

    country: Mapped[str] = mapped_column(
        String(2),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(
            "account_type IN ('savings', 'current', 'business')",
            name="ck_accounts_account_type",
        ),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_accounts_currency_format",
        ),
        CheckConstraint(
            "status IN ('active', 'frozen', 'closed')",
            name="ck_accounts_status",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    account_ref: Mapped[str] = mapped_column(
        String(30),
        unique=True,
        nullable=False,
    )

    customer_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    account_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
    )

    balance_minor_units: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(10),
        default="active",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    @validates("currency")
    def validate_supported_currency(self, key: str, currency: str) -> str:
        if currency not in SUPPORTED_ACCOUNT_CURRENCIES:
            raise ValueError(
                f"Unsupported currency {currency!r}; expected one of "
                f"{', '.join(sorted(SUPPORTED_ACCOUNT_CURRENCIES))}"
            )
        return currency


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(
            "transaction_type IN "
            "('transfer', 'card_payment', 'cash_withdrawal', 'deposit')",
            name="ck_transactions_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'completed', 'failed', 'reversed')",
            name="ck_transactions_status",
        ),
        CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_transactions_currency_format",
        ),
        CheckConstraint(
            "amount_minor_units > 0",
            name="ck_transactions_positive_amount",
        ),
        CheckConstraint(
            "transaction_type != 'transfer' OR "
            "(counterparty_account_id IS NOT NULL AND "
            "account_id != counterparty_account_id)",
            name="ck_transactions_transfer_accounts",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    transaction_ref: Mapped[str] = mapped_column(
        String(40),
        unique=True,
        nullable=False,
    )

    account_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    counterparty_account_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    amount_minor_units: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
    )

    transaction_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(10),
        default="pending",
        nullable=False,
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    merchant_ref: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )

    @validates("currency")
    def validate_supported_currency(self, key: str, currency: str) -> str:
        if currency not in SUPPORTED_ACCOUNT_CURRENCIES:
            raise ValueError(
                f"Unsupported currency {currency!r}; expected one of "
                f"{', '.join(sorted(SUPPORTED_ACCOUNT_CURRENCIES))}"
            )
        return currency