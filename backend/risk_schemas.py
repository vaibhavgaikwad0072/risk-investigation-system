from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, model_validator

from risk_data_loader import TargetTransaction
from risk_engine import RiskAssessment


class AssessmentRequest(BaseModel):
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_range(self) -> AssessmentRequest:
        for field_name in ("start_at", "end_at"):
            value = getattr(self, field_name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be later than start_at")
        return self

    def normalized_start(self) -> datetime:
        return self.start_at.astimezone(timezone.utc)

    def normalized_end(self) -> datetime:
        return self.end_at.astimezone(timezone.utc)


class TransactionEvidenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_ref: str
    occurred_at: datetime
    amount_minor_units: int
    currency: str
    status: str


class EvidenceMetricResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    value: int | str


class RuleFindingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rule_id: str
    rule_name: str
    severity: str
    transaction_refs: tuple[str, ...]
    transactions: tuple[TransactionEvidenceResponse, ...]
    account_refs: tuple[str, ...]
    observed: tuple[EvidenceMetricResponse, ...]
    threshold: tuple[EvidenceMetricResponse, ...]
    explanation: str
    configured_points: int
    applied_points: int
    scoring_family: str
    deduplicated_by: str | None


class RuleScoreContributionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rule_id: str
    configured_points: int
    applied_points: int
    scoring_family: str
    deduplicated_by: str | None


class RiskAssessmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_ref: str
    occurred_at: datetime
    amount_minor_units: int
    currency: str
    transaction_type: str
    status: str
    source_account_ref: str
    counterparty_account_ref: str | None
    score: int
    risk_band: str
    findings: tuple[RuleFindingResponse, ...]
    contributions: tuple[RuleScoreContributionResponse, ...]


class AssessmentResponse(BaseModel):
    start_at: datetime
    end_at: datetime
    assessment_count: int
    assessments: tuple[RiskAssessmentResponse, ...]


def serialize_assessment(
    assessment: RiskAssessment,
    target_transaction: TargetTransaction,
) -> RiskAssessmentResponse:
    return RiskAssessmentResponse(
        transaction_ref=assessment.transaction_ref,
        occurred_at=target_transaction.occurred_at.astimezone(timezone.utc),
        amount_minor_units=target_transaction.amount_minor_units,
        currency=target_transaction.currency,
        transaction_type=target_transaction.transaction_type,
        status=target_transaction.status,
        source_account_ref=target_transaction.source_account_ref,
        counterparty_account_ref=target_transaction.counterparty_account_ref,
        score=assessment.score,
        risk_band=assessment.risk_band,
        findings=tuple(
            RuleFindingResponse(
                rule_id=finding.rule_id,
                rule_name=finding.rule_name,
                severity=finding.severity,
                transaction_refs=finding.transaction_refs,
                transactions=tuple(
                    TransactionEvidenceResponse(
                        transaction_ref=evidence.transaction_ref,
                        occurred_at=evidence.occurred_at.astimezone(timezone.utc),
                        amount_minor_units=evidence.amount_minor_units,
                        currency=evidence.currency,
                        status=evidence.status,
                    )
                    for evidence in finding.transactions
                ),
                account_refs=finding.account_refs,
                observed=tuple(
                    EvidenceMetricResponse(name=metric.name, value=metric.value)
                    for metric in finding.observed
                ),
                threshold=tuple(
                    EvidenceMetricResponse(name=metric.name, value=metric.value)
                    for metric in finding.threshold
                ),
                explanation=finding.explanation,
                configured_points=finding.configured_points,
                applied_points=finding.applied_points,
                scoring_family=finding.scoring_family,
                deduplicated_by=finding.deduplicated_by,
            )
            for finding in assessment.findings
        ),
        contributions=tuple(
            RuleScoreContributionResponse(
                rule_id=contribution.rule_id,
                configured_points=contribution.configured_points,
                applied_points=contribution.applied_points,
                scoring_family=contribution.scoring_family,
                deduplicated_by=contribution.deduplicated_by,
            )
            for contribution in assessment.contributions
        ),
    )
