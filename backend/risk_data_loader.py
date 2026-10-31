from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, aliased

from models import Account, Transaction
from risk_engine import RiskConfig, TransactionInput


class AssessmentContextTooLarge(Exception):
    """Raised when complete rule context would exceed the configured row limit."""


class StoredDataIntegrityError(Exception):
    """Raised when persisted data cannot be mapped to valid risk-engine inputs."""


@dataclass(frozen=True)
class TargetTransaction:
    transaction_ref: str
    occurred_at: datetime
    amount_minor_units: int
    currency: str
    transaction_type: str
    status: str
    source_account_ref: str
    counterparty_account_ref: str | None


@dataclass(frozen=True)
class LoadedRiskData:
    transactions: tuple[TransactionInput, ...]
    target_transaction_refs: frozenset[str]
    target_transactions: tuple[TargetTransaction, ...]


def load_risk_data(
    session: Session,
    start_at: datetime,
    end_at: datetime,
    config: RiskConfig,
    maximum_rows: int,
) -> LoadedRiskData:
    source_account = aliased(Account)
    counterparty_account = aliased(Account)

    def transaction_query() -> Select:
        return (
            select(
                Transaction,
                source_account.account_ref,
                counterparty_account.account_ref,
            )
            .outerjoin(source_account, Transaction.account_id == source_account.id)
            .outerjoin(
                counterparty_account,
                Transaction.counterparty_account_id == counterparty_account.id,
            )
        )

    target_rows = _read_rows(
        session,
        transaction_query()
        .where(
            Transaction.occurred_at >= start_at,
            Transaction.occurred_at < end_at,
        )
        .order_by(Transaction.occurred_at, Transaction.id),
        maximum_rows,
    )
    target_refs = frozenset(row[0].transaction_ref for row in target_rows)
    if not target_rows:
        return LoadedRiskData((), frozenset(), ())

    target_account_ids = {row[0].account_id for row in target_rows}
    lookback_start = start_at - timedelta(seconds=config.history_window_seconds)
    history_rows = _read_rows(
        session,
        transaction_query()
        .where(
            Transaction.account_id.in_(target_account_ids),
            Transaction.status == "completed",
            Transaction.occurred_at >= lookback_start,
            Transaction.occurred_at < end_at,
        )
        .order_by(Transaction.occurred_at, Transaction.id),
        maximum_rows,
    )

    transfer_context_seconds = max(
        config.repeated_transfer_window_seconds,
        config.transfer_velocity_window_seconds,
        config.transfer_chain_window_seconds,
    )
    transfer_rows = _read_rows(
        session,
        transaction_query()
        .where(
            Transaction.transaction_type == "transfer",
            Transaction.status == "completed",
            Transaction.occurred_at
            >= start_at - timedelta(seconds=transfer_context_seconds),
            Transaction.occurred_at
            <= end_at + timedelta(seconds=transfer_context_seconds),
        )
        .order_by(Transaction.occurred_at, Transaction.id),
        maximum_rows,
    )

    rows_by_id: dict[int, tuple[Transaction, str | None, str | None]] = {}
    for row in (*target_rows, *history_rows, *transfer_rows):
        transaction, source_ref, counterparty_ref = row
        rows_by_id.setdefault(transaction.id, row)
    if len(rows_by_id) > maximum_rows:
        raise AssessmentContextTooLarge(
            "complete assessment context exceeds the configured row limit"
        )

    inputs: list[TransactionInput] = []
    target_transactions_by_ref: dict[str, TargetTransaction] = {}
    for transaction, source_ref, counterparty_ref in rows_by_id.values():
        if source_ref is None:
            raise StoredDataIntegrityError(
                f"transaction {transaction.transaction_ref!r} has no source account"
            )
        if (
            transaction.counterparty_account_id is not None
            and counterparty_ref is None
        ):
            raise StoredDataIntegrityError(
                f"transaction {transaction.transaction_ref!r} has an unresolved "
                "counterparty account"
            )
        if transaction.occurred_at.tzinfo is None:
            # SQLite strips timezone metadata; persisted DateTime values are UTC.
            occurred_at = transaction.occurred_at.replace(tzinfo=timezone.utc)
        else:
            occurred_at = transaction.occurred_at.astimezone(timezone.utc)
        if transaction.transaction_ref in target_refs:
            target_transactions_by_ref[transaction.transaction_ref] = (
                TargetTransaction(
                    transaction_ref=transaction.transaction_ref,
                    occurred_at=occurred_at,
                    amount_minor_units=transaction.amount_minor_units,
                    currency=transaction.currency,
                    transaction_type=transaction.transaction_type,
                    status=transaction.status,
                    source_account_ref=source_ref,
                    counterparty_account_ref=counterparty_ref,
                )
            )
        inputs.append(
            TransactionInput(
                transaction_ref=transaction.transaction_ref,
                account_ref=source_ref,
                amount_minor_units=transaction.amount_minor_units,
                currency=transaction.currency,
                transaction_type=transaction.transaction_type,
                status=transaction.status,
                occurred_at=occurred_at,
                counterparty_account_ref=counterparty_ref,
            )
        )

    return LoadedRiskData(
        transactions=tuple(inputs),
        target_transaction_refs=target_refs,
        target_transactions=tuple(
            target_transactions_by_ref[row[0].transaction_ref] for row in target_rows
        ),
    )


def _read_rows(
    session: Session,
    statement: Select,
    maximum_rows: int,
) -> list[tuple[Transaction, str | None, str | None]]:
    rows = session.execute(statement.limit(maximum_rows + 1)).all()
    if len(rows) > maximum_rows:
        raise AssessmentContextTooLarge(
            "complete assessment context exceeds the configured row limit"
        )
    return rows
