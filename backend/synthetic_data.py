from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    SUPPORTED_ACCOUNT_CURRENCIES,
    Account,
    Customer,
    Transaction,
)

ScenarioKind = Literal[
    "rapid_transfers",
    "repeated_transfers",
    "large_amount",
    "multi_account_movement",
]

_MAX_RECORDS_PER_KIND = 100_000
_MAX_SQLITE_INTEGER = 2**63 - 1
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,12}$")
_COUNTRIES = ("IN", "US", "DE", "GB", "CA", "AU")
_ACCOUNT_TYPES = ("savings", "current", "business")
_TRANSACTION_TYPES = (
    "transfer",
    "card_payment",
    "cash_withdrawal",
    "deposit",
)
_STATUSES = ("completed", "pending", "failed", "reversed")


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    kind: ScenarioKind
    transaction_count: int
    amount_minor_units: int = 5_000
    interval_seconds: int = 60


@dataclass(frozen=True)
class GeneratorConfig:
    seed: int
    run_id: str
    customer_count: int
    account_count: int
    transaction_count: int
    start_at: datetime
    activity_window_seconds: int = 7 * 24 * 60 * 60
    large_amount_threshold_minor_units: int = 1_000_000
    scenarios: tuple[ScenarioSpec, ...] = ()


@dataclass(frozen=True)
class CustomerData:
    customer_ref: str
    full_name: str
    country: str
    created_at: datetime


@dataclass(frozen=True)
class AccountData:
    account_ref: str
    customer_ref: str
    account_type: str
    currency: str
    balance_minor_units: int
    status: str
    created_at: datetime


@dataclass(frozen=True)
class TransactionData:
    transaction_ref: str
    account_ref: str
    counterparty_account_ref: str | None
    amount_minor_units: int
    currency: str
    transaction_type: str
    status: str
    occurred_at: datetime
    created_at: datetime
    merchant_ref: str | None


@dataclass(frozen=True)
class GeneratedDataset:
    run_id: str
    customers: tuple[CustomerData, ...]
    accounts: tuple[AccountData, ...]
    transactions: tuple[TransactionData, ...]
    scenario_labels: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class PersistedDataset:
    customer_ids: dict[str, int]
    account_ids: dict[str, int]
    transaction_ids: dict[str, int]


def validate_config(config: GeneratorConfig) -> None:
    for field_name in ("seed", "customer_count", "account_count", "transaction_count"):
        value = getattr(config, field_name)
        if type(value) is not int:
            raise ValueError(f"{field_name} must be an integer")

    if not isinstance(config.run_id, str) or not _RUN_ID_PATTERN.fullmatch(
        config.run_id
    ):
        raise ValueError("run_id must contain 1-12 letters, digits, '_' or '-'")

    for field_name in ("customer_count", "account_count", "transaction_count"):
        value = getattr(config, field_name)
        if value < 0 or value > _MAX_RECORDS_PER_KIND:
            raise ValueError(
                f"{field_name} must be between 0 and {_MAX_RECORDS_PER_KIND}"
            )

    if config.account_count and not config.customer_count:
        raise ValueError("at least one customer is required when generating accounts")
    if config.transaction_count and not config.account_count:
        raise ValueError("at least one account is required when generating transactions")
    if (
        config.start_at.tzinfo is None
        or config.start_at.utcoffset() is None
    ):
        raise ValueError("start_at must be a timezone-aware datetime")
    if type(config.activity_window_seconds) is not int or config.activity_window_seconds < 1:
        raise ValueError("activity_window_seconds must be a positive integer")
    if (
        type(config.large_amount_threshold_minor_units) is not int
        or config.large_amount_threshold_minor_units < 1
    ):
        raise ValueError("large amount threshold must be a positive integer")
    if not (
        1
        <= config.large_amount_threshold_minor_units
        < _MAX_SQLITE_INTEGER
    ):
        raise ValueError(
            "large amount threshold must be positive and below SQLite integer capacity"
        )

    scenario_ids: set[str] = set()
    scenario_transaction_count = 0
    needs_three_accounts = False
    needs_two_accounts = False
    for scenario in config.scenarios:
        if scenario.scenario_id in scenario_ids:
            raise ValueError(f"duplicate scenario_id: {scenario.scenario_id}")
        scenario_ids.add(scenario.scenario_id)
        if not isinstance(scenario.scenario_id, str) or not _RUN_ID_PATTERN.fullmatch(
            scenario.scenario_id
        ):
            raise ValueError(
                "scenario_id must contain 1-12 letters, digits, '_' or '-'"
            )
        if scenario.kind not in (
            "rapid_transfers",
            "repeated_transfers",
            "large_amount",
            "multi_account_movement",
        ):
            raise ValueError(f"unsupported scenario kind: {scenario.kind}")
        if type(scenario.transaction_count) is not int or scenario.transaction_count < 1:
            raise ValueError("scenario transaction_count must be a positive integer")
        if scenario.kind in ("rapid_transfers", "repeated_transfers"):
            if scenario.transaction_count < 2:
                raise ValueError(f"{scenario.kind} requires at least two transactions")
            needs_two_accounts = True
        if scenario.kind == "multi_account_movement":
            if scenario.transaction_count < 2:
                raise ValueError("multi_account_movement requires at least two transactions")
            needs_three_accounts = True
        if type(scenario.amount_minor_units) is not int or not (
            1 <= scenario.amount_minor_units <= _MAX_SQLITE_INTEGER
        ):
            raise ValueError("scenario amount must be a positive SQLite integer")
        if type(scenario.interval_seconds) is not int or scenario.interval_seconds < 1:
            raise ValueError("scenario interval_seconds must be a positive integer")
        if scenario.kind == "rapid_transfers" and scenario.interval_seconds > 300:
            raise ValueError("rapid transfer interval_seconds must not exceed 300")
        if scenario.kind == "large_amount" and (
            scenario.transaction_count != 1
            or scenario.amount_minor_units
            <= config.large_amount_threshold_minor_units
        ):
            raise ValueError(
                "large_amount requires one transaction above the configured threshold"
            )
        duration = (scenario.transaction_count - 1) * scenario.interval_seconds
        if duration > config.activity_window_seconds:
            raise ValueError("scenario duration exceeds the activity window")
        scenario_transaction_count += scenario.transaction_count

    if scenario_transaction_count > config.transaction_count:
        raise ValueError("scenario transactions exceed configured transaction_count")
    minimum_accounts = 3 if needs_three_accounts else 2 if needs_two_accounts else 0
    if config.account_count < minimum_accounts:
        raise ValueError(
            f"configured scenarios require at least {minimum_accounts} accounts"
        )


def generate_dataset(config: GeneratorConfig) -> GeneratedDataset:
    validate_config(config)
    rng = random.Random(config.seed)
    start_at = config.start_at.astimezone(timezone.utc)
    scenario_transaction_count = sum(
        scenario.transaction_count for scenario in config.scenarios
    )
    ordinary_transaction_count = config.transaction_count - scenario_transaction_count

    customers = tuple(
        CustomerData(
            customer_ref=f"SYN-{config.run_id}-C-{index:06d}",
            full_name=f"Synthetic Customer {index:06d}",
            country=rng.choice(_COUNTRIES),
            created_at=start_at - timedelta(days=2),
        )
        for index in range(1, config.customer_count + 1)
    )

    scenario_account_count = _scenario_account_count(config.scenarios)
    scenario_currency = rng.choice(sorted(SUPPORTED_ACCOUNT_CURRENCIES))
    accounts = tuple(
        AccountData(
            account_ref=f"SYN-{config.run_id}-A-{index:06d}",
            customer_ref=rng.choice(customers).customer_ref,
            account_type=rng.choice(_ACCOUNT_TYPES),
            currency=(
                scenario_currency
                if index <= scenario_account_count
                else rng.choice(sorted(SUPPORTED_ACCOUNT_CURRENCIES))
            ),
            balance_minor_units=0,
            status=(
                "active"
                if index <= scenario_account_count
                else rng.choices(
                    ("active", "frozen", "closed"),
                    weights=(95, 4, 1),
                    k=1,
                )[0]
            ),
            created_at=start_at - timedelta(days=1),
        )
        for index in range(1, config.account_count + 1)
    )
    accounts_by_ref = {account.account_ref: account for account in accounts}
    account_refs_by_currency: dict[str, list[str]] = {}
    for account in accounts:
        account_refs_by_currency.setdefault(account.currency, []).append(
            account.account_ref
        )

    transactions: list[TransactionData] = []
    scenario_labels: dict[str, tuple[str, ...]] = {}
    transaction_sequence = 1

    for scenario_index, scenario in enumerate(config.scenarios):
        scenario_refs: list[str] = []
        account_pool = [
            account.account_ref for account in accounts[:scenario_account_count]
        ]
        if scenario.kind in ("rapid_transfers", "repeated_transfers"):
            source_ref, destination_ref = account_pool[:2]
        for item_index in range(scenario.transaction_count):
            transaction_ref = _transaction_ref(
                config.run_id,
                transaction_sequence,
            )
            transaction_sequence += 1
            occurred_at = start_at + timedelta(
                seconds=item_index * scenario.interval_seconds
            )
            currency = accounts_by_ref[account_pool[0]].currency
            account_ref: str
            counterparty_ref: str | None
            transaction_type: str
            merchant_ref: str | None = None

            if scenario.kind in ("rapid_transfers", "repeated_transfers"):
                account_ref = source_ref
                counterparty_ref = destination_ref
                transaction_type = "transfer"
            elif scenario.kind == "multi_account_movement":
                account_ref = account_pool[item_index % 3]
                counterparty_ref = account_pool[(item_index + 1) % 3]
                transaction_type = "transfer"
            else:
                account_ref = account_pool[0]
                counterparty_ref = None
                transaction_type = "card_payment"
                merchant_ref = f"SYN-MERCHANT-{config.run_id}-{scenario_index + 1:04d}"

            transactions.append(
                TransactionData(
                    transaction_ref=transaction_ref,
                    account_ref=account_ref,
                    counterparty_account_ref=counterparty_ref,
                    amount_minor_units=scenario.amount_minor_units,
                    currency=currency,
                    transaction_type=transaction_type,
                    status="completed",
                    occurred_at=occurred_at,
                    created_at=occurred_at,
                    merchant_ref=merchant_ref,
                )
            )
            scenario_refs.append(transaction_ref)
        for transaction_ref in scenario_refs:
            scenario_labels[transaction_ref] = (scenario.scenario_id,)

    for _ in range(ordinary_transaction_count):
        transaction_ref = _transaction_ref(config.run_id, transaction_sequence)
        transaction_sequence += 1
        account = rng.choice(accounts)
        same_currency_refs = account_refs_by_currency[account.currency]
        other_account_refs = [
            ref for ref in same_currency_refs if ref != account.account_ref
        ]
        possible_types = _TRANSACTION_TYPES
        if not other_account_refs:
            possible_types = tuple(
                kind for kind in _TRANSACTION_TYPES if kind != "transfer"
            )
        transaction_type = rng.choice(possible_types)
        counterparty_ref = (
            rng.choice(other_account_refs)
            if transaction_type == "transfer"
            else None
        )
        occurred_at = start_at + timedelta(
            seconds=rng.randrange(config.activity_window_seconds)
        )
        transaction = TransactionData(
            transaction_ref=transaction_ref,
            account_ref=account.account_ref,
            counterparty_account_ref=counterparty_ref,
            amount_minor_units=rng.randint(100, 100_000),
            currency=account.currency,
            transaction_type=transaction_type,
            status=rng.choices(
                _STATUSES,
                weights=(85, 8, 5, 2),
                k=1,
            )[0],
            occurred_at=occurred_at,
            created_at=occurred_at,
            merchant_ref=(
                f"SYN-MERCHANT-{config.run_id}-{transaction_sequence:08d}"
                if transaction_type == "card_payment"
                else None
            ),
        )
        transactions.append(transaction)

    dataset = GeneratedDataset(
        run_id=config.run_id,
        customers=customers,
        accounts=accounts,
        transactions=tuple(transactions),
        scenario_labels=scenario_labels,
    )
    validate_dataset(dataset)
    return dataset


def validate_dataset(dataset: GeneratedDataset) -> None:
    customer_refs = {customer.customer_ref for customer in dataset.customers}
    account_by_ref = {account.account_ref: account for account in dataset.accounts}
    transaction_refs = [transaction.transaction_ref for transaction in dataset.transactions]

    if len(customer_refs) != len(dataset.customers):
        raise ValueError("generated customer references are not unique")
    if len(account_by_ref) != len(dataset.accounts):
        raise ValueError("generated account references are not unique")
    if len(set(transaction_refs)) != len(transaction_refs):
        raise ValueError("generated transaction references are not unique")

    for account in dataset.accounts:
        if account.customer_ref not in customer_refs:
            raise ValueError(f"account {account.account_ref} has an unknown customer")
        if (
            account.created_at.tzinfo is None
            or account.created_at.utcoffset() is None
        ):
            raise ValueError("generated timestamps must be timezone-aware")
        if account.currency not in SUPPORTED_ACCOUNT_CURRENCIES:
            raise ValueError(f"account {account.account_ref} has unsupported currency")
        if type(account.balance_minor_units) is not int:
            raise ValueError("account balances must be integer minor units")

    for customer in dataset.customers:
        if len(customer.country) != 2 or customer.country != customer.country.upper():
            raise ValueError("generated customer country must be two uppercase letters")
        if customer.created_at.tzinfo is None or customer.created_at.utcoffset() is None:
            raise ValueError("generated timestamps must be timezone-aware")

    for transaction in dataset.transactions:
        account = account_by_ref.get(transaction.account_ref)
        if account is None:
            raise ValueError(
                f"transaction {transaction.transaction_ref} has an unknown account"
            )
        if transaction.counterparty_account_ref is not None:
            counterparty = account_by_ref.get(transaction.counterparty_account_ref)
            if counterparty is None:
                raise ValueError(
                    f"transaction {transaction.transaction_ref} has an unknown "
                    "counterparty account"
                )
            if counterparty.currency != account.currency:
                raise ValueError("transaction account currencies do not match")
            if transaction.account_ref == transaction.counterparty_account_ref:
                raise ValueError("transaction accounts must be distinct")
        if transaction.currency != account.currency:
            raise ValueError("transaction currency must match its account")
        if transaction.currency not in SUPPORTED_ACCOUNT_CURRENCIES:
            raise ValueError("transaction currency is unsupported")
        if type(transaction.amount_minor_units) is not int or not (
            1 <= transaction.amount_minor_units <= _MAX_SQLITE_INTEGER
        ):
            raise ValueError("transaction amount must be a positive SQLite integer")
        if transaction.transaction_type not in _TRANSACTION_TYPES:
            raise ValueError("generated transaction has an invalid type")
        if transaction.status not in _STATUSES:
            raise ValueError("generated transaction has an invalid status")
        if (
            transaction.transaction_type == "transfer"
            and transaction.counterparty_account_ref is None
        ):
            raise ValueError("transfers require a counterparty account")
        if (
            transaction.occurred_at.tzinfo is None
            or transaction.occurred_at.utcoffset() is None
            or transaction.created_at.tzinfo is None
            or transaction.created_at.utcoffset() is None
        ):
            raise ValueError("generated timestamps must be timezone-aware")


def persist_dataset(dataset: GeneratedDataset, session: Session) -> PersistedDataset:
    validate_dataset(dataset)
    if session.in_transaction():
        raise RuntimeError("persist_dataset requires a session with no active transaction")

    customer_refs = [customer.customer_ref for customer in dataset.customers]
    account_refs = [account.account_ref for account in dataset.accounts]
    transaction_refs = [transaction.transaction_ref for transaction in dataset.transactions]
    with session.begin():
        _reject_existing_refs(session, Customer.customer_ref, customer_refs, "customer")
        _reject_existing_refs(session, Account.account_ref, account_refs, "account")
        _reject_existing_refs(
            session,
            Transaction.transaction_ref,
            transaction_refs,
            "transaction",
        )

        customer_rows = [
            Customer(
                customer_ref=item.customer_ref,
                full_name=item.full_name,
                country=item.country,
                created_at=item.created_at,
            )
            for item in dataset.customers
        ]
        session.add_all(customer_rows)
        session.flush()
        customer_ids = {
            customer.customer_ref: customer.id for customer in customer_rows
        }

        account_rows = [
            Account(
                account_ref=item.account_ref,
                customer_id=customer_ids[item.customer_ref],
                account_type=item.account_type,
                currency=item.currency,
                balance_minor_units=item.balance_minor_units,
                status=item.status,
                created_at=item.created_at,
            )
            for item in dataset.accounts
        ]
        session.add_all(account_rows)
        session.flush()
        account_ids = {account.account_ref: account.id for account in account_rows}

        transaction_rows = [
            Transaction(
                transaction_ref=item.transaction_ref,
                account_id=account_ids[item.account_ref],
                counterparty_account_id=(
                    account_ids[item.counterparty_account_ref]
                    if item.counterparty_account_ref is not None
                    else None
                ),
                amount_minor_units=item.amount_minor_units,
                currency=item.currency,
                transaction_type=item.transaction_type,
                status=item.status,
                occurred_at=item.occurred_at,
                created_at=item.created_at,
                merchant_ref=item.merchant_ref,
            )
            for item in dataset.transactions
        ]
        session.add_all(transaction_rows)
        session.flush()
        transaction_ids = {
            transaction.transaction_ref: transaction.id
            for transaction in transaction_rows
        }

    return PersistedDataset(
        customer_ids=customer_ids,
        account_ids=account_ids,
        transaction_ids=transaction_ids,
    )


def _scenario_account_count(scenarios: tuple[ScenarioSpec, ...]) -> int:
    if any(item.kind == "multi_account_movement" for item in scenarios):
        return 3
    if any(item.kind in ("rapid_transfers", "repeated_transfers") for item in scenarios):
        return 2
    return 1 if scenarios else 0


def _transaction_ref(run_id: str, sequence: int) -> str:
    return f"SYN-{run_id}-TX-{sequence:08d}"


def _reject_existing_refs(
    session: Session,
    column,
    refs: list[str],
    entity_name: str,
) -> None:
    for start in range(0, len(refs), 500):
        batch = refs[start : start + 500]
        if not batch:
            continue
        existing_ref = session.scalar(select(column).where(column.in_(batch)).limit(1))
        if existing_ref is not None:
            raise ValueError(
                f"{entity_name} reference {existing_ref!r} already exists; "
                "choose a new run_id"
            )
