from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TypeAlias

SUPPORTED_CURRENCIES = frozenset({"INR", "USD", "EUR"})
_AMOUNT_ANOMALY_FAMILY = "amount_anomaly"
_TRANSFER_PATTERN_FAMILY = "transfer_pattern"
_EVALUATED_STATUSES = frozenset({"pending", "completed", "failed"})
_HISTORY_STATUS = "completed"
_HISTORY_RULE_ID = "R006"
_LARGE_AMOUNT_RULE_ID = "R001"
_REPEATED_TRANSFER_RULE_ID = "R002"
_TRANSFER_VELOCITY_RULE_ID = "R003"
_MULTI_ACCOUNT_CHAIN_RULE_ID = "R004"

EvidenceValue: TypeAlias = int | str


@dataclass(frozen=True)
class TransactionInput:
    transaction_ref: str
    account_ref: str
    amount_minor_units: int
    currency: str
    transaction_type: str
    status: str
    occurred_at: datetime
    counterparty_account_ref: str | None = None


@dataclass(frozen=True)
class CurrencyThreshold:
    currency: str
    large_amount_minor_units: int
    historical_absolute_floor_minor_units: int


@dataclass(frozen=True)
class RuleContributionConfig:
    rule_id: str
    points: int
    scoring_family: str


@dataclass(frozen=True)
class RiskBand:
    name: str
    minimum_score: int
    maximum_score: int


@dataclass(frozen=True)
class RiskConfig:
    currency_thresholds: tuple[CurrencyThreshold, ...] = (
        CurrencyThreshold("INR", 1_000_000, 100_000),
        CurrencyThreshold("USD", 1_000_000, 100_000),
        CurrencyThreshold("EUR", 1_000_000, 100_000),
    )
    contributions: tuple[RuleContributionConfig, ...] = (
        RuleContributionConfig(_LARGE_AMOUNT_RULE_ID, 25, _AMOUNT_ANOMALY_FAMILY),
        RuleContributionConfig(
            _REPEATED_TRANSFER_RULE_ID,
            25,
            _TRANSFER_PATTERN_FAMILY,
        ),
        RuleContributionConfig(
            _TRANSFER_VELOCITY_RULE_ID,
            20,
            _TRANSFER_PATTERN_FAMILY,
        ),
        RuleContributionConfig(
            _MULTI_ACCOUNT_CHAIN_RULE_ID,
            30,
            _TRANSFER_PATTERN_FAMILY,
        ),
        RuleContributionConfig(_HISTORY_RULE_ID, 20, _AMOUNT_ANOMALY_FAMILY),
    )
    bands: tuple[RiskBand, ...] = (
        RiskBand("low", 0, 24),
        RiskBand("medium", 25, 49),
        RiskBand("high", 50, 74),
        RiskBand("critical", 75, 100),
    )
    history_window_seconds: int = 30 * 24 * 60 * 60
    minimum_history_count: int = 20
    historical_multiplier: int = 3
    repeated_transfer_window_seconds: int = 10 * 60
    minimum_repeated_transfer_count: int = 3
    transfer_velocity_window_seconds: int = 5 * 60
    minimum_velocity_transfer_count: int = 5
    minimum_velocity_distinct_recipient_count: int = 3
    transfer_chain_window_seconds: int = 30 * 60
    minimum_transfer_chain_length: int = 2
    minimum_transfer_chain_account_count: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.currency_thresholds, tuple) or not self.currency_thresholds:
            raise ValueError("currency_thresholds must be a non-empty tuple")
        if any(not isinstance(item, CurrencyThreshold) for item in self.currency_thresholds):
            raise ValueError("currency_thresholds must contain CurrencyThreshold values")
        for item in self.currency_thresholds:
            if not isinstance(item.currency, str) or item.currency not in SUPPORTED_CURRENCIES:
                raise ValueError(
                    f"unsupported or malformed currency threshold {item.currency!r}"
                )
        currencies = [item.currency for item in self.currency_thresholds]
        if len(set(currencies)) != len(currencies):
            raise ValueError("currency thresholds must have unique currencies")
        if set(currencies) != SUPPORTED_CURRENCIES:
            raise ValueError(
                "currency thresholds must define INR, USD, and EUR exactly"
            )
        for item in self.currency_thresholds:
            if (
                type(item.large_amount_minor_units) is not int
                or item.large_amount_minor_units < 0
            ):
                raise ValueError("large-amount thresholds must be non-negative integers")
            if (
                type(item.historical_absolute_floor_minor_units) is not int
                or item.historical_absolute_floor_minor_units < 0
            ):
                raise ValueError("historical floors must be non-negative integers")

        if not isinstance(self.contributions, tuple) or not self.contributions:
            raise ValueError("contributions must be a non-empty tuple")
        if any(
            not isinstance(item, RuleContributionConfig)
            for item in self.contributions
        ):
            raise ValueError(
                "contributions must contain RuleContributionConfig values"
            )
        rule_ids = [item.rule_id for item in self.contributions]
        if any(not isinstance(rule_id, str) for rule_id in rule_ids):
            raise ValueError("rule IDs must be strings")
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("rule contribution IDs must be unique")
        if set(rule_ids) != {
            _LARGE_AMOUNT_RULE_ID,
            _REPEATED_TRANSFER_RULE_ID,
            _TRANSFER_VELOCITY_RULE_ID,
            _MULTI_ACCOUNT_CHAIN_RULE_ID,
            _HISTORY_RULE_ID,
        }:
            raise ValueError(
                "contributions must configure R001, R002, R003, R004, and R006 exactly"
            )
        for item in self.contributions:
            if type(item.points) is not int or not 0 <= item.points <= 100:
                raise ValueError("rule points must be integers between 0 and 100")
            if (
                not isinstance(item.scoring_family, str)
                or not item.scoring_family.strip()
            ):
                raise ValueError("scoring family must not be empty")
        if (
            self.contribution_for(_LARGE_AMOUNT_RULE_ID).scoring_family
            != self.contribution_for(_HISTORY_RULE_ID).scoring_family
        ):
            raise ValueError("R001 and R006 must share the amount-anomaly family")
        transfer_family = self.contribution_for(
            _REPEATED_TRANSFER_RULE_ID
        ).scoring_family
        if (
            transfer_family == _AMOUNT_ANOMALY_FAMILY
            or self.contribution_for(_TRANSFER_VELOCITY_RULE_ID).scoring_family
            != transfer_family
            or self.contribution_for(_MULTI_ACCOUNT_CHAIN_RULE_ID).scoring_family
            != transfer_family
        ):
            raise ValueError(
                "R002, R003, and R004 must share the transfer-pattern family"
            )

        if not isinstance(self.bands, tuple) or not self.bands:
            raise ValueError("bands must be a non-empty tuple")
        if any(not isinstance(item, RiskBand) for item in self.bands):
            raise ValueError("bands must contain RiskBand values")
        expected_minimum = 0
        band_names: set[str] = set()
        for band in self.bands:
            if (
                not isinstance(band.name, str)
                or not band.name.strip()
                or band.name in band_names
            ):
                raise ValueError("risk band names must be non-empty and unique")
            band_names.add(band.name)
            if (
                type(band.minimum_score) is not int
                or type(band.maximum_score) is not int
                or band.minimum_score != expected_minimum
                or band.maximum_score < band.minimum_score
                or band.maximum_score > 100
            ):
                raise ValueError(
                    "risk bands must be ordered, contiguous, and within 0..100"
                )
            expected_minimum = band.maximum_score + 1
        if expected_minimum != 101:
            raise ValueError("risk bands must cover scores 0 through 100")

        for field_name in (
            "history_window_seconds",
            "minimum_history_count",
            "historical_multiplier",
            "repeated_transfer_window_seconds",
            "minimum_repeated_transfer_count",
            "transfer_velocity_window_seconds",
            "minimum_velocity_transfer_count",
            "minimum_velocity_distinct_recipient_count",
            "transfer_chain_window_seconds",
            "minimum_transfer_chain_length",
            "minimum_transfer_chain_account_count",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.minimum_transfer_chain_length < 2:
            raise ValueError("minimum_transfer_chain_length must be at least 2")
        if self.minimum_transfer_chain_account_count < 3:
            raise ValueError("minimum_transfer_chain_account_count must be at least 3")

    def threshold_for(self, currency: str) -> CurrencyThreshold:
        for item in self.currency_thresholds:
            if item.currency == currency:
                return item
        raise ValueError(f"no thresholds configured for currency {currency!r}")

    def contribution_for(self, rule_id: str) -> RuleContributionConfig:
        for item in self.contributions:
            if item.rule_id == rule_id:
                return item
        raise ValueError(f"no score contribution configured for rule {rule_id!r}")


@dataclass(frozen=True)
class TransactionEvidence:
    transaction_ref: str
    occurred_at: datetime
    amount_minor_units: int
    currency: str
    status: str


@dataclass(frozen=True)
class EvidenceMetric:
    name: str
    value: EvidenceValue


@dataclass(frozen=True)
class RuleFinding:
    rule_id: str
    rule_name: str
    severity: str
    transaction_refs: tuple[str, ...]
    transactions: tuple[TransactionEvidence, ...]
    account_refs: tuple[str, ...]
    observed: tuple[EvidenceMetric, ...]
    threshold: tuple[EvidenceMetric, ...]
    explanation: str
    configured_points: int
    applied_points: int
    scoring_family: str
    deduplicated_by: str | None


@dataclass(frozen=True)
class RuleScoreContribution:
    rule_id: str
    configured_points: int
    applied_points: int
    scoring_family: str
    deduplicated_by: str | None


@dataclass(frozen=True)
class RiskAssessment:
    transaction_ref: str
    score: int
    risk_band: str
    findings: tuple[RuleFinding, ...]
    contributions: tuple[RuleScoreContribution, ...]


def evaluate_risk(
    transactions: tuple[TransactionInput, ...] | list[TransactionInput],
    config: RiskConfig = RiskConfig(),
) -> tuple[RiskAssessment, ...]:
    ordered = _validate_and_sort_inputs(transactions, config)
    findings_by_transaction: dict[str, list[RuleFinding]] = {
        transaction.transaction_ref: [] for transaction in ordered
    }

    for window in _repeated_transfer_windows(ordered, config):
        contribution = config.contribution_for(_REPEATED_TRANSFER_RULE_ID)
        source_ref = window[0].account_ref
        destination_ref = window[0].counterparty_account_ref
        window_start = window[0].occurred_at.astimezone(timezone.utc)
        window_end = window[-1].occurred_at.astimezone(timezone.utc)
        finding = RuleFinding(
            rule_id=_REPEATED_TRANSFER_RULE_ID,
            rule_name="Repeated transfers between the same accounts",
            severity="medium",
            transaction_refs=tuple(item.transaction_ref for item in window),
            transactions=tuple(_transaction_evidence(item) for item in window),
            account_refs=(source_ref, destination_ref),
            observed=(
                EvidenceMetric("source_account_ref", source_ref),
                EvidenceMetric("destination_account_ref", destination_ref),
                EvidenceMetric("observed_count", len(window)),
                EvidenceMetric("window_start_utc", window_start.isoformat()),
                EvidenceMetric("window_end_utc", window_end.isoformat()),
            ),
            threshold=(
                EvidenceMetric(
                    "minimum_transaction_count",
                    config.minimum_repeated_transfer_count,
                ),
                EvidenceMetric(
                    "window_seconds",
                    config.repeated_transfer_window_seconds,
                ),
                EvidenceMetric("window_boundary", "inclusive"),
                EvidenceMetric("comparison", "at_least"),
            ),
            explanation=(
                f"{len(window)} completed transfers occurred from {source_ref} "
                f"to {destination_ref} within an inclusive "
                f"{config.repeated_transfer_window_seconds}-second window "
                f"({window_start.isoformat()} through {window_end.isoformat()}); "
                f"the configured minimum is "
                f"{config.minimum_repeated_transfer_count}."
            ),
            configured_points=contribution.points,
            applied_points=0,
            scoring_family=contribution.scoring_family,
            deduplicated_by=None,
        )
        for transaction in window:
            findings_by_transaction[transaction.transaction_ref].append(finding)

    for window in _transfer_velocity_windows(ordered, config):
        contribution = config.contribution_for(_TRANSFER_VELOCITY_RULE_ID)
        source_ref = window[0].account_ref
        recipient_refs = tuple(
            sorted(
                {
                    item.counterparty_account_ref
                    for item in window
                    if item.counterparty_account_ref is not None
                }
            )
        )
        window_start = window[0].occurred_at.astimezone(timezone.utc)
        window_end = window[-1].occurred_at.astimezone(timezone.utc)
        finding = RuleFinding(
            rule_id=_TRANSFER_VELOCITY_RULE_ID,
            rule_name="Short-window transfer velocity",
            severity="medium",
            transaction_refs=tuple(item.transaction_ref for item in window),
            transactions=tuple(_transaction_evidence(item) for item in window),
            account_refs=(source_ref, *recipient_refs),
            observed=(
                EvidenceMetric("source_account_ref", source_ref),
                EvidenceMetric("observed_count", len(window)),
                EvidenceMetric("distinct_recipient_count", len(recipient_refs)),
                EvidenceMetric("window_start_utc", window_start.isoformat()),
                EvidenceMetric("window_end_utc", window_end.isoformat()),
            ),
            threshold=(
                EvidenceMetric(
                    "minimum_transaction_count",
                    config.minimum_velocity_transfer_count,
                ),
                EvidenceMetric(
                    "minimum_distinct_recipient_count",
                    config.minimum_velocity_distinct_recipient_count,
                ),
                EvidenceMetric("window_seconds", config.transfer_velocity_window_seconds),
                EvidenceMetric("window_boundary", "inclusive"),
                EvidenceMetric("comparison", "at_least"),
            ),
            explanation=(
                f"{len(window)} completed transfers from {source_ref} to "
                f"{len(recipient_refs)} distinct recipient accounts occurred "
                f"within an inclusive {config.transfer_velocity_window_seconds}-"
                f"second window ({window_start.isoformat()} through "
                f"{window_end.isoformat()}); configured minimums are "
                f"{config.minimum_velocity_transfer_count} transfers and "
                f"{config.minimum_velocity_distinct_recipient_count} recipients."
            ),
            configured_points=contribution.points,
            applied_points=0,
            scoring_family=contribution.scoring_family,
            deduplicated_by=None,
        )
        for transaction in window:
            findings_by_transaction[transaction.transaction_ref].append(finding)

    for path in _transfer_chain_paths(ordered, config):
        contribution = config.contribution_for(_MULTI_ACCOUNT_CHAIN_RULE_ID)
        account_path = (
            path[0].account_ref,
            *(transaction.counterparty_account_ref for transaction in path),
        )
        path_start = path[0].occurred_at.astimezone(timezone.utc)
        path_end = path[-1].occurred_at.astimezone(timezone.utc)
        finding = RuleFinding(
            rule_id=_MULTI_ACCOUNT_CHAIN_RULE_ID,
            rule_name="Multi-account transfer chain",
            severity="medium",
            transaction_refs=tuple(item.transaction_ref for item in path),
            transactions=tuple(_transaction_evidence(item) for item in path),
            account_refs=account_path,
            observed=(
                EvidenceMetric("path_length", len(path)),
                EvidenceMetric("distinct_account_count", len(set(account_path))),
                EvidenceMetric("path_start_utc", path_start.isoformat()),
                EvidenceMetric("path_end_utc", path_end.isoformat()),
                EvidenceMetric("elapsed_seconds", int((path_end - path_start).total_seconds())),
            ),
            threshold=(
                EvidenceMetric(
                    "minimum_transfer_count",
                    config.minimum_transfer_chain_length,
                ),
                EvidenceMetric(
                    "minimum_distinct_account_count",
                    config.minimum_transfer_chain_account_count,
                ),
                EvidenceMetric("window_seconds", config.transfer_chain_window_seconds),
                EvidenceMetric("window_boundary", "inclusive"),
                EvidenceMetric("timestamp_order", "strictly_increasing"),
            ),
            explanation=(
                f"{len(path)} completed transfers form an ordered path across "
                f"{len(set(account_path))} distinct accounts from {account_path[0]} "
                f"to {account_path[-1]} within an inclusive "
                f"{config.transfer_chain_window_seconds}-second window "
                f"({path_start.isoformat()} through {path_end.isoformat()}); "
                f"configured minimums are {config.minimum_transfer_chain_length} "
                f"transfers and {config.minimum_transfer_chain_account_count} "
                "distinct accounts."
            ),
            configured_points=contribution.points,
            applied_points=0,
            scoring_family=contribution.scoring_family,
            deduplicated_by=None,
        )
        for transaction in path:
            findings_by_transaction[transaction.transaction_ref].append(finding)

    for transaction in ordered:
        if transaction.status not in _EVALUATED_STATUSES:
            continue
        currency_threshold = config.threshold_for(transaction.currency)
        if transaction.amount_minor_units > currency_threshold.large_amount_minor_units:
            contribution = config.contribution_for(_LARGE_AMOUNT_RULE_ID)
            evidence = _transaction_evidence(transaction)
            findings_by_transaction[transaction.transaction_ref].append(
                RuleFinding(
                    rule_id=_LARGE_AMOUNT_RULE_ID,
                    rule_name="Large transaction amount",
                    severity="high",
                    transaction_refs=(transaction.transaction_ref,),
                    transactions=(evidence,),
                    account_refs=(transaction.account_ref,),
                    observed=(
                        EvidenceMetric("amount_minor_units", transaction.amount_minor_units),
                        EvidenceMetric("currency", transaction.currency),
                        EvidenceMetric("status", transaction.status),
                    ),
                    threshold=(
                        EvidenceMetric(
                            "large_amount_minor_units",
                            currency_threshold.large_amount_minor_units,
                        ),
                        EvidenceMetric("currency", transaction.currency),
                        EvidenceMetric("comparison", "strictly_greater_than"),
                    ),
                    explanation=(
                        f"Transaction {transaction.transaction_ref} amount "
                        f"{transaction.amount_minor_units} {transaction.currency} "
                        f"is greater than the configured threshold "
                        f"{currency_threshold.large_amount_minor_units} "
                        f"{transaction.currency}."
                    ),
                    configured_points=contribution.points,
                    applied_points=0,
                    scoring_family=contribution.scoring_family,
                    deduplicated_by=None,
                )
            )

        if transaction.status != _HISTORY_STATUS:
            continue
        historical = _eligible_history(transaction, ordered, config.history_window_seconds)
        if len(historical) < config.minimum_history_count:
            continue
        median = _median_minor_units(
            [item.amount_minor_units for item in historical]
        )
        threshold = currency_threshold.historical_absolute_floor_minor_units
        median_threshold = median * config.historical_multiplier
        if not (
            Decimal(transaction.amount_minor_units) > median_threshold
            and transaction.amount_minor_units > threshold
        ):
            continue

        contribution = config.contribution_for(_HISTORY_RULE_ID)
        evidence_transactions = tuple(
            _transaction_evidence(item) for item in (*historical, transaction)
        )
        findings_by_transaction[transaction.transaction_ref].append(
            RuleFinding(
                rule_id=_HISTORY_RULE_ID,
                rule_name="Historical amount anomaly",
                severity="medium",
                transaction_refs=tuple(
                    item.transaction_ref for item in (*historical, transaction)
                ),
                transactions=evidence_transactions,
                account_refs=(transaction.account_ref,),
                observed=(
                    EvidenceMetric("amount_minor_units", transaction.amount_minor_units),
                    EvidenceMetric("currency", transaction.currency),
                    EvidenceMetric("baseline_count", len(historical)),
                    EvidenceMetric("historical_median_minor_units", str(median)),
                ),
                threshold=(
                    EvidenceMetric("history_window_seconds", config.history_window_seconds),
                    EvidenceMetric("minimum_history_count", config.minimum_history_count),
                    EvidenceMetric("median_multiplier", config.historical_multiplier),
                    EvidenceMetric("median_threshold_minor_units", str(median_threshold)),
                    EvidenceMetric(
                        "absolute_floor_minor_units",
                        currency_threshold.historical_absolute_floor_minor_units,
                    ),
                    EvidenceMetric("comparison", "strictly_greater_than_both"),
                ),
                explanation=(
                    f"Completed transaction {transaction.transaction_ref} amount "
                    f"{transaction.amount_minor_units} {transaction.currency} "
                    f"exceeds both {config.historical_multiplier} times the "
                    f"historical median ({median_threshold} {transaction.currency}) "
                    f"and the absolute floor "
                    f"{currency_threshold.historical_absolute_floor_minor_units} "
                    f"{transaction.currency}, based on {len(historical)} earlier "
                    f"completed transactions."
                ),
                configured_points=contribution.points,
                applied_points=0,
                scoring_family=contribution.scoring_family,
                deduplicated_by=None,
            )
        )

    assessments: list[RiskAssessment] = []
    for transaction in ordered:
        findings = findings_by_transaction[transaction.transaction_ref]
        finalized_findings, contributions, score = _apply_score_deduplication(
            findings,
            config,
        )
        band = next(
            item.name
            for item in config.bands
            if item.minimum_score <= score <= item.maximum_score
        )
        assessments.append(
            RiskAssessment(
                transaction_ref=transaction.transaction_ref,
                score=score,
                risk_band=band,
                findings=finalized_findings,
                contributions=contributions,
            )
        )
    return tuple(assessments)


def _validate_and_sort_inputs(
    transactions: tuple[TransactionInput, ...] | list[TransactionInput],
    config: RiskConfig,
) -> tuple[TransactionInput, ...]:
    if not isinstance(config, RiskConfig):
        raise ValueError("config must be a RiskConfig")
    if not isinstance(transactions, (tuple, list)):
        raise ValueError("transactions must be a tuple or list")

    refs: set[str] = set()
    for transaction in transactions:
        if not isinstance(transaction, TransactionInput):
            raise ValueError("all inputs must be TransactionInput values")
        if (
            not isinstance(transaction.transaction_ref, str)
            or not transaction.transaction_ref.strip()
            or transaction.transaction_ref in refs
        ):
            raise ValueError("transaction references must be non-empty and unique")
        refs.add(transaction.transaction_ref)
        if (
            not isinstance(transaction.account_ref, str)
            or not transaction.account_ref.strip()
        ):
            raise ValueError("account references must be non-empty")
        if type(transaction.amount_minor_units) is not int or transaction.amount_minor_units <= 0:
            raise ValueError("transaction amounts must be positive integer minor units")
        if not isinstance(transaction.currency, str):
            raise ValueError("transaction currency must be a supported currency code")
        config.threshold_for(transaction.currency)
        if not isinstance(transaction.transaction_type, str) or transaction.transaction_type not in {
            "transfer",
            "card_payment",
            "cash_withdrawal",
            "deposit",
        }:
            raise ValueError(f"invalid transaction type {transaction.transaction_type!r}")
        if transaction.counterparty_account_ref is not None and (
            not isinstance(transaction.counterparty_account_ref, str)
            or not transaction.counterparty_account_ref.strip()
            or transaction.counterparty_account_ref == transaction.account_ref
        ):
            raise ValueError("counterparty account references must be non-empty and different")
        if (
            transaction.transaction_type == "transfer"
            and transaction.counterparty_account_ref is None
        ):
            raise ValueError("transfer transactions require a counterparty account reference")
        if not isinstance(transaction.status, str) or transaction.status not in {
            "pending",
            "completed",
            "failed",
            "reversed",
        }:
            raise ValueError(f"invalid transaction status {transaction.status!r}")
        if (
            not isinstance(transaction.occurred_at, datetime)
            or
            transaction.occurred_at.tzinfo is None
            or transaction.occurred_at.utcoffset() is None
        ):
            raise ValueError("transaction occurred_at must be timezone-aware")

    return tuple(
        sorted(
            transactions,
            key=lambda item: (
                item.occurred_at.astimezone(timezone.utc),
                item.transaction_ref,
            ),
        )
    )


def _eligible_history(
    target: TransactionInput,
    transactions: tuple[TransactionInput, ...],
    history_window_seconds: int,
) -> tuple[TransactionInput, ...]:
    target_time = target.occurred_at.astimezone(timezone.utc)
    window_start = target_time - timedelta(seconds=history_window_seconds)
    eligible = [
        item
        for item in transactions
        if item.transaction_ref != target.transaction_ref
        and item.status == _HISTORY_STATUS
        and item.account_ref == target.account_ref
        and item.currency == target.currency
        and item.transaction_type == target.transaction_type
        and window_start
        <= item.occurred_at.astimezone(timezone.utc)
        < target_time
    ]
    return tuple(
        sorted(
            eligible,
            key=lambda item: (
                item.occurred_at.astimezone(timezone.utc),
                item.transaction_ref,
            ),
        )
    )


def _repeated_transfer_windows(
    transactions: tuple[TransactionInput, ...],
    config: RiskConfig,
) -> tuple[tuple[TransactionInput, ...], ...]:
    pairs: dict[tuple[str, str], list[TransactionInput]] = {}
    for transaction in transactions:
        if (
            transaction.transaction_type == "transfer"
            and transaction.status == _HISTORY_STATUS
            and transaction.counterparty_account_ref is not None
        ):
            pair = (transaction.account_ref, transaction.counterparty_account_ref)
            pairs.setdefault(pair, []).append(transaction)

    qualifying_windows: list[tuple[TransactionInput, ...]] = []
    window_delta = timedelta(seconds=config.repeated_transfer_window_seconds)
    for pair in sorted(pairs):
        pair_transactions = sorted(
            pairs[pair],
            key=lambda item: (
                item.occurred_at.astimezone(timezone.utc),
                item.transaction_ref,
            ),
        )
        for start_index in range(len(pair_transactions)):
            anchor_time = pair_transactions[start_index].occurred_at.astimezone(
                timezone.utc
            )
            end_index = start_index
            while end_index < len(pair_transactions):
                event_time = pair_transactions[end_index].occurred_at.astimezone(
                    timezone.utc
                )
                if event_time - anchor_time > window_delta:
                    break
                end_index += 1

            window = tuple(pair_transactions[start_index:end_index])
            is_extendable_from_previous = (
                start_index > 0
                and pair_transactions[end_index - 1].occurred_at.astimezone(
                    timezone.utc
                )
                - pair_transactions[start_index - 1].occurred_at.astimezone(
                    timezone.utc
                )
                <= window_delta
            )
            # Keep maximal windows; adjacent qualifying windows may overlap.
            if (
                len(window) >= config.minimum_repeated_transfer_count
                and not is_extendable_from_previous
            ):
                qualifying_windows.append(window)

    return tuple(
        sorted(
            qualifying_windows,
            key=lambda window: (
                window[0].occurred_at.astimezone(timezone.utc),
                window[0].account_ref,
                window[0].counterparty_account_ref or "",
                tuple(item.transaction_ref for item in window),
            ),
        )
    )


def _transfer_velocity_windows(
    transactions: tuple[TransactionInput, ...],
    config: RiskConfig,
) -> tuple[tuple[TransactionInput, ...], ...]:
    sources: dict[str, list[TransactionInput]] = {}
    for transaction in transactions:
        if (
            transaction.transaction_type == "transfer"
            and transaction.status == _HISTORY_STATUS
            and transaction.counterparty_account_ref is not None
        ):
            sources.setdefault(transaction.account_ref, []).append(transaction)

    qualifying_windows: list[tuple[TransactionInput, ...]] = []
    window_delta = timedelta(seconds=config.transfer_velocity_window_seconds)
    for source_ref in sorted(sources):
        source_transactions = sorted(
            sources[source_ref],
            key=lambda item: (
                item.occurred_at.astimezone(timezone.utc),
                item.transaction_ref,
            ),
        )
        for start_index in range(len(source_transactions)):
            anchor_time = source_transactions[start_index].occurred_at.astimezone(
                timezone.utc
            )
            end_index = start_index
            while end_index < len(source_transactions):
                event_time = source_transactions[end_index].occurred_at.astimezone(
                    timezone.utc
                )
                if event_time - anchor_time > window_delta:
                    break
                end_index += 1

            window = tuple(source_transactions[start_index:end_index])
            recipients = {
                item.counterparty_account_ref
                for item in window
                if item.counterparty_account_ref is not None
            }
            is_extendable_from_previous = (
                start_index > 0
                and source_transactions[end_index - 1].occurred_at.astimezone(
                    timezone.utc
                )
                - source_transactions[start_index - 1].occurred_at.astimezone(
                    timezone.utc
                )
                <= window_delta
            )
            if (
                len(window) >= config.minimum_velocity_transfer_count
                and len(recipients) >= config.minimum_velocity_distinct_recipient_count
                and not is_extendable_from_previous
            ):
                qualifying_windows.append(window)

    return tuple(
        sorted(
            qualifying_windows,
            key=lambda window: (
                window[0].occurred_at.astimezone(timezone.utc),
                window[0].account_ref,
                tuple(item.transaction_ref for item in window),
            ),
        )
    )


def _transfer_chain_paths(
    transactions: tuple[TransactionInput, ...],
    config: RiskConfig,
) -> tuple[tuple[TransactionInput, ...], ...]:
    outgoing: dict[str, list[TransactionInput]] = {}
    eligible = tuple(
        transaction
        for transaction in transactions
        if transaction.transaction_type == "transfer"
        and transaction.status == _HISTORY_STATUS
        and transaction.counterparty_account_ref is not None
    )
    for transaction in eligible:
        outgoing.setdefault(transaction.account_ref, []).append(transaction)

    for edges in outgoing.values():
        edges.sort(
            key=lambda item: (
                item.occurred_at.astimezone(timezone.utc),
                item.transaction_ref,
            )
        )

    candidates: dict[tuple[str, ...], tuple[TransactionInput, ...]] = {}
    window_delta = timedelta(seconds=config.transfer_chain_window_seconds)

    def extend_path(path: tuple[TransactionInput, ...]) -> None:
        distinct_accounts = {
            path[0].account_ref,
            *(item.counterparty_account_ref for item in path),
        }
        if (
            len(path) >= config.minimum_transfer_chain_length
            and len(distinct_accounts) >= config.minimum_transfer_chain_account_count
        ):
            candidates[tuple(item.transaction_ref for item in path)] = path

        last = path[-1]
        destination = last.counterparty_account_ref
        if destination is None:
            return
        path_start = path[0].occurred_at.astimezone(timezone.utc)
        last_time = last.occurred_at.astimezone(timezone.utc)
        for edge in outgoing.get(destination, ()):
            edge_time = edge.occurred_at.astimezone(timezone.utc)
            if (
                edge_time <= last_time
                or edge_time - path_start > window_delta
            ):
                continue
            extend_path((*path, edge))

    for transaction in eligible:
        extend_path((transaction,))

    candidate_refs = tuple(candidates)
    maximal_paths = [
        path
        for refs, path in candidates.items()
        if not any(
            len(other_refs) > len(refs)
            and any(
                other_refs[index : index + len(refs)] == refs
                for index in range(len(other_refs) - len(refs) + 1)
            )
            for other_refs in candidate_refs
        )
    ]
    return tuple(
        sorted(
            maximal_paths,
            key=lambda path: (
                path[0].occurred_at.astimezone(timezone.utc),
                path[0].account_ref,
                tuple(item.transaction_ref for item in path),
            ),
        )
    )


def _median_minor_units(values: list[int]) -> Decimal:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return Decimal(ordered[midpoint])
    return Decimal(ordered[midpoint - 1] + ordered[midpoint]) / Decimal(2)


def _transaction_evidence(transaction: TransactionInput) -> TransactionEvidence:
    return TransactionEvidence(
        transaction_ref=transaction.transaction_ref,
        occurred_at=transaction.occurred_at.astimezone(timezone.utc),
        amount_minor_units=transaction.amount_minor_units,
        currency=transaction.currency,
        status=transaction.status,
    )


def _apply_score_deduplication(
    findings: list[RuleFinding],
    config: RiskConfig,
) -> tuple[
    tuple[RuleFinding, ...],
    tuple[RuleScoreContribution, ...],
    int,
]:
    winners: dict[str, RuleFinding] = {}
    for finding in findings:
        current = winners.get(finding.scoring_family)
        if current is None or (
            finding.configured_points,
            finding.rule_id,
        ) > (
            current.configured_points,
            current.rule_id,
        ):
            winners[finding.scoring_family] = finding

    finalized: list[RuleFinding] = []
    contributions: list[RuleScoreContribution] = []
    for finding in sorted(findings, key=lambda item: item.rule_id):
        winner = winners[finding.scoring_family]
        is_winner = finding is winner
        applied_points = finding.configured_points if is_winner else 0
        deduplicated_by = None if is_winner else winner.rule_id
        finalized.append(
            RuleFinding(
                rule_id=finding.rule_id,
                rule_name=finding.rule_name,
                severity=finding.severity,
                transaction_refs=finding.transaction_refs,
                transactions=finding.transactions,
                account_refs=finding.account_refs,
                observed=finding.observed,
                threshold=finding.threshold,
                explanation=finding.explanation,
                configured_points=finding.configured_points,
                applied_points=applied_points,
                scoring_family=finding.scoring_family,
                deduplicated_by=deduplicated_by,
            )
        )
        contributions.append(
            RuleScoreContribution(
                rule_id=finding.rule_id,
                configured_points=finding.configured_points,
                applied_points=applied_points,
                scoring_family=finding.scoring_family,
                deduplicated_by=deduplicated_by,
            )
        )

    score = min(100, sum(item.applied_points for item in contributions))
    return tuple(finalized), tuple(contributions), score
