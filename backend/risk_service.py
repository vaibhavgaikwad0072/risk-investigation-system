from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from risk_data_loader import (
    StoredDataIntegrityError,
    TargetTransaction,
    load_risk_data,
)
from risk_engine import RiskAssessment, RiskConfig, evaluate_risk


@dataclass(frozen=True)
class AssessmentLimits:
    maximum_target_range_seconds: int = 7 * 24 * 60 * 60
    maximum_loaded_rows: int = 50_000

    def __post_init__(self) -> None:
        if (
            type(self.maximum_target_range_seconds) is not int
            or self.maximum_target_range_seconds < 1
        ):
            raise ValueError("maximum_target_range_seconds must be a positive integer")
        if type(self.maximum_loaded_rows) is not int or self.maximum_loaded_rows < 1:
            raise ValueError("maximum_loaded_rows must be a positive integer")


DEFAULT_ASSESSMENT_LIMITS = AssessmentLimits()


@dataclass(frozen=True)
class AssessmentRun:
    start_at: datetime
    end_at: datetime
    assessments: tuple[RiskAssessment, ...]
    target_transactions: tuple[TargetTransaction, ...]


class AssessmentRangeTooLarge(Exception):
    """Raised when the requested target interval exceeds service limits."""


def run_assessment(
    session: Session,
    start_at: datetime,
    end_at: datetime,
    config: RiskConfig = RiskConfig(),
    limits: AssessmentLimits = DEFAULT_ASSESSMENT_LIMITS,
) -> AssessmentRun:
    start_utc = _normalize_request_timestamp(start_at, "start_at")
    end_utc = _normalize_request_timestamp(end_at, "end_at")
    if end_utc <= start_utc:
        raise ValueError("end_at must be later than start_at")
    if end_utc - start_utc > timedelta(
        seconds=limits.maximum_target_range_seconds
    ):
        raise AssessmentRangeTooLarge(
            "requested target range exceeds the configured maximum"
        )

    loaded = load_risk_data(
        session=session,
        start_at=start_utc,
        end_at=end_utc,
        config=config,
        maximum_rows=limits.maximum_loaded_rows,
    )
    if not loaded.target_transaction_refs:
        return AssessmentRun(start_utc, end_utc, (), ())

    try:
        all_assessments = evaluate_risk(loaded.transactions, config)
    except ValueError as exc:
        raise StoredDataIntegrityError(
            "stored transaction data is incompatible with risk evaluation"
        ) from exc

    target_assessments = tuple(
        assessment
        for assessment in all_assessments
        if assessment.transaction_ref in loaded.target_transaction_refs
    )
    return AssessmentRun(
        start_utc,
        end_utc,
        target_assessments,
        loaded.target_transactions,
    )


def _normalize_request_timestamp(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)
