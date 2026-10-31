import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

from risk_engine import (
    CurrencyThreshold,
    EvidenceMetric,
    RiskBand,
    RiskConfig,
    RuleContributionConfig,
    TransactionInput,
    evaluate_risk,
)


class RiskEngineTests(unittest.TestCase):
    def setUp(self):
        self.config = RiskConfig(
            currency_thresholds=(
                CurrencyThreshold("INR", 1_000, 100),
                CurrencyThreshold("USD", 2_000, 200),
                CurrencyThreshold("EUR", 3_000, 300),
            )
        )
        self.start = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)

    def _transaction(
        self,
        ref="TX-1",
        amount=500,
        currency="INR",
        status="completed",
        occurred_at=None,
        account="ACC-1",
        transaction_type="card_payment",
        counterparty=None,
    ):
        return TransactionInput(
            transaction_ref=ref,
            account_ref=account,
            amount_minor_units=amount,
            currency=currency,
            transaction_type=transaction_type,
            status=status,
            occurred_at=occurred_at or self.start,
            counterparty_account_ref=counterparty,
        )

    def _assessment(self, transaction, config=None, transactions=None):
        inputs = transactions if transactions is not None else (transaction,)
        return next(
            item
            for item in evaluate_risk(inputs, config or self.config)
            if item.transaction_ref == transaction.transaction_ref
        )

    def _history(self, count=20, target_amount=301, **target_overrides):
        target_time = target_overrides.pop("occurred_at", self.start)
        history = [
            self._transaction(
                ref=f"HIST-{index:02d}",
                amount=100,
                occurred_at=target_time - timedelta(minutes=20 - index),
            )
            for index in range(count)
        ]
        target = self._transaction(
            ref="TARGET",
            amount=target_amount,
            occurred_at=target_time,
            **target_overrides,
        )
        return history, target

    def _transfer(
        self,
        ref,
        offset_seconds,
        source="ACC-1",
        destination="ACC-2",
        status="completed",
        currency="INR",
        transaction_type="transfer",
        amount=100,
    ):
        return self._transaction(
            ref=ref,
            amount=amount,
            currency=currency,
            status=status,
            occurred_at=self.start + timedelta(seconds=offset_seconds),
            account=source,
            transaction_type=transaction_type,
            counterparty=destination,
        )

    def test_r001_below_equal_and_above_threshold(self):
        below = self._transaction(amount=999)
        equal = self._transaction(ref="TX-EQUAL", amount=1_000)
        above = self._transaction(ref="TX-ABOVE", amount=1_001)

        self.assertEqual(self._assessment(below).findings, ())
        self.assertEqual(self._assessment(equal).findings, ())
        finding = self._assessment(above).findings[0]
        self.assertEqual(finding.rule_id, "R001")
        self.assertEqual(finding.transaction_refs, ("TX-ABOVE",))
        self.assertIn(EvidenceMetric("amount_minor_units", 1_001), finding.observed)
        self.assertIn(EvidenceMetric("large_amount_minor_units", 1_000), finding.threshold)

    def test_r001_uses_currency_specific_thresholds(self):
        transactions = (
            self._transaction(ref="INR", amount=1_001),
            self._transaction(ref="USD", amount=1_001, currency="USD"),
            self._transaction(ref="EUR", amount=3_001, currency="EUR"),
        )
        assessments = {
            item.transaction_ref: item
            for item in evaluate_risk(transactions, self.config)
        }

        self.assertEqual(len(assessments["INR"].findings), 1)
        self.assertEqual(assessments["USD"].findings, ())
        self.assertEqual(len(assessments["EUR"].findings), 1)

    def test_r001_status_eligibility(self):
        transactions = tuple(
            self._transaction(
                ref=status.upper(),
                amount=1_001,
                status=status,
            )
            for status in ("pending", "completed", "failed", "reversed")
        )
        assessments = {
            item.transaction_ref: item
            for item in evaluate_risk(transactions, self.config)
        }

        for status in ("PENDING", "COMPLETED", "FAILED"):
            self.assertEqual(assessments[status].findings[0].rule_id, "R001")
        self.assertEqual(assessments["REVERSED"].findings, ())

    def test_r006_returns_no_finding_with_insufficient_history(self):
        no_history, no_history_target = self._history(count=0)
        no_history_assessment = self._assessment(
            no_history_target,
            transactions=(*no_history, no_history_target),
        )
        self.assertFalse(
            any(item.rule_id == "R006" for item in no_history_assessment.findings)
        )

        history, target = self._history(count=19)
        assessment = self._assessment(target, transactions=(*history, target))

        self.assertFalse(any(item.rule_id == "R006" for item in assessment.findings))

    def test_r006_only_evaluates_completed_target_transactions(self):
        history, target = self._history(count=20)
        for status in ("pending", "failed", "reversed"):
            with self.subTest(status=status):
                noncompleted = replace(target, status=status)
                assessment = self._assessment(
                    noncompleted,
                    transactions=(*history, noncompleted),
                )
                self.assertFalse(
                    any(item.rule_id == "R006" for item in assessment.findings)
                )

    def test_r006_triggers_with_exactly_minimum_eligible_history(self):
        history, target = self._history(count=20)
        finding = next(
            item
            for item in self._assessment(target, transactions=(*history, target)).findings
            if item.rule_id == "R006"
        )

        self.assertEqual(len(finding.transaction_refs), 21)
        self.assertIn(EvidenceMetric("baseline_count", 20), finding.observed)
        self.assertIn(
            EvidenceMetric("historical_median_minor_units", "100"),
            finding.observed,
        )

    def test_r006_history_window_includes_exact_boundary_and_excludes_older(self):
        config = replace(self.config, history_window_seconds=30 * 24 * 60 * 60)
        boundary = self.start - timedelta(days=30)
        eligible = [
            self._transaction(
                ref=f"BOUNDARY-{index}",
                amount=100,
                occurred_at=boundary + timedelta(seconds=index),
            )
            for index in range(20)
        ]
        too_old = self._transaction(
            ref="TOO-OLD",
            amount=100,
            occurred_at=boundary - timedelta(microseconds=1),
        )
        target = self._transaction(ref="TARGET", amount=301, occurred_at=self.start)

        finding = next(
            item
            for item in self._assessment(
                target,
                config=config,
                transactions=(*eligible, too_old, target),
            ).findings
            if item.rule_id == "R006"
        )
        self.assertIn("BOUNDARY-0", finding.transaction_refs)
        self.assertNotIn("TOO-OLD", finding.transaction_refs)
        self.assertIn(EvidenceMetric("baseline_count", 20), finding.observed)

    def test_r006_uses_only_strictly_earlier_rows_and_excludes_target(self):
        history, target = self._history(count=20, target_amount=301)
        same_time = self._transaction(
            ref="SAME-TIME",
            amount=100,
            occurred_at=self.start,
        )
        future = self._transaction(
            ref="FUTURE",
            amount=100,
            occurred_at=self.start + timedelta(seconds=1),
        )
        assessment = self._assessment(
            target,
            transactions=(*history, same_time, target, future),
        )
        finding = next(item for item in assessment.findings if item.rule_id == "R006")

        self.assertEqual(len(finding.transaction_refs), 21)
        self.assertNotIn("SAME-TIME", finding.transaction_refs)
        self.assertNotIn("FUTURE", finding.transaction_refs)
        self.assertEqual(finding.transaction_refs[-1], "TARGET")

    def test_r006_filters_by_account_currency_type_and_completed_status(self):
        history, target = self._history(count=19)
        excluded = (
            self._transaction(ref="OTHER-ACCOUNT", account="ACC-2", amount=100),
            self._transaction(ref="OTHER-CURRENCY", currency="USD", amount=100),
            self._transaction(
                ref="OTHER-TYPE",
                transaction_type="transfer",
                amount=100,
                counterparty="ACC-2",
            ),
            self._transaction(ref="PENDING", status="pending", amount=100),
        )
        assessment = self._assessment(
            target,
            transactions=(*history, *excluded, target),
        )

        self.assertFalse(any(item.rule_id == "R006" for item in assessment.findings))

    def test_r006_requires_strictly_exceeding_median_multiplier_and_floor(self):
        history, target_equal_median = self._history(count=20, target_amount=300)
        self.assertFalse(
            any(
                item.rule_id == "R006"
                for item in self._assessment(
                    target_equal_median,
                    transactions=(*history, target_equal_median),
                ).findings
            )
        )
        history, target_above_median = self._history(count=20, target_amount=301)
        self.assertTrue(
            any(
                item.rule_id == "R006"
                for item in self._assessment(
                    target_above_median,
                    transactions=(*history, target_above_median),
                ).findings
            )
        )

        high_floor_config = replace(
            self.config,
            currency_thresholds=(
                CurrencyThreshold("INR", 1_000, 400),
                CurrencyThreshold("USD", 2_000, 200),
                CurrencyThreshold("EUR", 3_000, 300),
            ),
        )
        history, target_equal_floor = self._history(count=20, target_amount=400)
        self.assertFalse(
            any(
                item.rule_id == "R006"
                for item in self._assessment(
                    target_equal_floor,
                    config=high_floor_config,
                    transactions=(*history, target_equal_floor),
                ).findings
            )
        )

        history, target_above_both = self._history(count=20, target_amount=401)
        self.assertTrue(
            any(
                item.rule_id == "R006"
                for item in self._assessment(
                    target_above_both,
                    config=high_floor_config,
                    transactions=(*history, target_above_both),
                ).findings
            )
        )

    def test_r006_uses_odd_and_even_medians(self):
        for count, values, expected_median, target_amount in (
            (20, list(range(100, 120)), "109.5", 329),
            (21, list(range(100, 121)), "110", 331),
        ):
            with self.subTest(count=count):
                history = [
                    self._transaction(
                        ref=f"HIST-{count}-{index}",
                        amount=amount,
                        occurred_at=self.start - timedelta(minutes=count - index),
                    )
                    for index, amount in enumerate(values)
                ]
                target = self._transaction(
                    ref=f"TARGET-{count}",
                    amount=target_amount,
                    occurred_at=self.start,
                )
                finding = next(
                    item
                    for item in self._assessment(
                        target,
                        transactions=(*history, target),
                    ).findings
                    if item.rule_id == "R006"
                )
                self.assertIn(
                    EvidenceMetric("historical_median_minor_units", expected_median),
                    finding.observed,
                )

    def test_r006_evidence_is_traceable_to_target_and_history(self):
        history, target = self._history(count=20)
        finding = next(
            item
            for item in self._assessment(target, transactions=(*history, target)).findings
            if item.rule_id == "R006"
        )

        self.assertEqual(finding.rule_name, "Historical amount anomaly")
        self.assertEqual(finding.account_refs, ("ACC-1",))
        self.assertEqual(finding.transactions[-1].transaction_ref, "TARGET")
        self.assertEqual(finding.transactions[-1].amount_minor_units, 301)
        self.assertIn("20 earlier completed transactions", finding.explanation)
        self.assertIn("median_multiplier", {item.name for item in finding.threshold})
        self.assertIn("absolute_floor_minor_units", {item.name for item in finding.threshold})
        self.assertEqual(
            finding.transaction_refs,
            tuple(item.transaction_ref for item in finding.transactions),
        )
        self.assertEqual(
            set(finding.transaction_refs),
            {item.transaction_ref for item in (*history, target)},
        )
        self.assertTrue(
            all(item.occurred_at.tzinfo == timezone.utc for item in finding.transactions)
        )
        self.assertNotIn("fraud", finding.explanation.lower())
        self.assertNotIn("money laundering", finding.explanation.lower())

    def test_evidence_is_stable_and_normalizes_timestamp_to_utc(self):
        local_time = datetime(
            2026,
            1,
            31,
            17,
            tzinfo=timezone(timedelta(hours=5)),
        )
        transaction = self._transaction(
            ref="UTC-EVIDENCE",
            amount=1_001,
            occurred_at=local_time,
        )
        first = self._assessment(transaction)
        second = self._assessment(transaction)

        self.assertEqual(first.findings, second.findings)
        evidence = first.findings[0].transactions[0]
        self.assertEqual(evidence.transaction_ref, "UTC-EVIDENCE")
        self.assertEqual(evidence.occurred_at, self.start)
        self.assertEqual(evidence.occurred_at.tzinfo, timezone.utc)
        self.assertEqual(evidence.amount_minor_units, 1_001)
        self.assertEqual(evidence.currency, "INR")
        self.assertEqual(evidence.status, "completed")
        self.assertNotIn("fraud", first.findings[0].explanation.lower())

    def test_output_is_stable_when_input_order_changes(self):
        history, target = self._history(count=20)
        transactions = (*history, target)

        forward = evaluate_risk(transactions, self.config)
        reverse = evaluate_risk(tuple(reversed(transactions)), self.config)

        self.assertEqual(forward, reverse)

    def test_r001_and_r006_findings_are_preserved_but_points_are_deduplicated(self):
        history, target = self._history(count=20, target_amount=1_001)
        assessment = self._assessment(target, transactions=(*history, target))

        self.assertEqual(
            {item.rule_id for item in assessment.findings},
            {"R001", "R006"},
        )
        self.assertEqual(assessment.score, 25)
        contributions = {item.rule_id: item for item in assessment.contributions}
        self.assertEqual(contributions["R001"].applied_points, 25)
        self.assertEqual(contributions["R001"].configured_points, 25)
        self.assertEqual(contributions["R006"].applied_points, 0)
        self.assertEqual(contributions["R006"].configured_points, 20)
        self.assertEqual(contributions["R006"].deduplicated_by, "R001")
        finding_by_rule = {item.rule_id: item for item in assessment.findings}
        self.assertEqual(finding_by_rule["R001"].applied_points, 25)
        self.assertEqual(finding_by_rule["R001"].configured_points, 25)
        self.assertEqual(finding_by_rule["R006"].applied_points, 0)
        self.assertEqual(finding_by_rule["R006"].configured_points, 20)

    def test_exact_risk_band_boundaries_and_score_cap(self):
        expected = {
            0: "low",
            24: "low",
            25: "medium",
            49: "medium",
            50: "high",
            74: "high",
            75: "critical",
            100: "critical",
        }
        for points, band in expected.items():
            with self.subTest(points=points):
                config = replace(
                    self.config,
                    contributions=(
                        RuleContributionConfig("R001", points, "amount_anomaly"),
                        RuleContributionConfig("R002", 0, "transfer_pattern"),
                        RuleContributionConfig("R003", 0, "transfer_pattern"),
                        RuleContributionConfig("R004", 0, "transfer_pattern"),
                        RuleContributionConfig("R006", 0, "amount_anomaly"),
                    ),
                )
                assessment = self._assessment(
                    self._transaction(amount=1_001),
                    config=config,
                )
                self.assertEqual(assessment.score, points)
                self.assertEqual(assessment.risk_band, band)

        capped_config = replace(
            self.config,
            contributions=(
                RuleContributionConfig("R001", 100, "amount_anomaly"),
                RuleContributionConfig("R002", 0, "transfer_pattern"),
                RuleContributionConfig("R003", 0, "transfer_pattern"),
                RuleContributionConfig("R004", 0, "transfer_pattern"),
                RuleContributionConfig("R006", 100, "amount_anomaly"),
            ),
        )
        self.assertEqual(
            self._assessment(
                self._transaction(amount=1_001),
                config=capped_config,
            ).score,
            100,
        )

    def test_empty_input_returns_no_assessments(self):
        self.assertEqual(evaluate_risk((), self.config), ())

    def test_no_findings_produce_zero_score_and_configured_lowest_band(self):
        assessment = self._assessment(self._transaction(amount=1))
        self.assertEqual(assessment.findings, ())
        self.assertEqual(assessment.contributions, ())
        self.assertEqual(assessment.score, 0)
        self.assertEqual(assessment.risk_band, "low")

        custom_bands = replace(
            self.config,
            bands=(
                RiskBand("minimal", 0, 9),
                RiskBand("medium", 10, 49),
                RiskBand("high", 50, 74),
                RiskBand("critical", 75, 100),
            ),
        )
        self.assertEqual(
            self._assessment(
                self._transaction(ref="NO-FINDING", amount=1),
                config=custom_bands,
            ).risk_band,
            "minimal",
        )

    def test_r002_triggers_at_minimum_count_and_not_one_fewer(self):
        two = (
            self._transfer("T-1", 0),
            self._transfer("T-2", 60),
        )
        three = (*two, self._transfer("T-3", 120))

        self.assertFalse(
            any(
                finding.rule_id == "R002"
                for assessment in evaluate_risk(two, self.config)
                for finding in assessment.findings
            )
        )
        assessment = self._assessment(three[0], transactions=three)
        finding = next(item for item in assessment.findings if item.rule_id == "R002")
        self.assertEqual(finding.transaction_refs, ("T-1", "T-2", "T-3"))
        self.assertIn(EvidenceMetric("observed_count", 3), finding.observed)

    def test_r002_includes_exact_window_boundary_and_excludes_just_outside(self):
        exact_boundary = (
            self._transfer("BOUNDARY-1", 0),
            self._transfer("BOUNDARY-2", 300),
            self._transfer("BOUNDARY-3", 600),
        )
        outside = (
            self._transfer("OUTSIDE-1", 0),
            self._transfer("OUTSIDE-2", 300),
            self._transfer("OUTSIDE-3", 601),
        )

        finding = next(
            item
            for item in self._assessment(
                exact_boundary[0],
                transactions=exact_boundary,
            ).findings
            if item.rule_id == "R002"
        )
        self.assertEqual(finding.transaction_refs, tuple(item.transaction_ref for item in exact_boundary))
        self.assertFalse(
            any(
                item.rule_id == "R002"
                for assessment in evaluate_risk(outside, self.config)
                for item in assessment.findings
            )
        )
        self.assertIn(EvidenceMetric("window_boundary", "inclusive"), finding.threshold)

    def test_r002_preserves_direction_and_separates_account_pairs(self):
        transfers = (
            self._transfer("AB-1", 0),
            self._transfer("AB-2", 30),
            self._transfer("AB-3", 60),
            self._transfer("BA-1", 0, source="ACC-2", destination="ACC-1"),
            self._transfer("BA-2", 30, source="ACC-2", destination="ACC-1"),
            self._transfer("BA-3", 60, source="ACC-2", destination="ACC-1"),
            self._transfer("AC-1", 0, destination="ACC-3"),
            self._transfer("AC-2", 30, destination="ACC-3"),
            self._transfer("AC-3", 60, destination="ACC-3"),
            self._transfer("CB-1", 0, source="ACC-3", destination="ACC-2"),
            self._transfer("CB-2", 30, source="ACC-3", destination="ACC-2"),
            self._transfer("CB-3", 60, source="ACC-3", destination="ACC-2"),
        )
        assessments = evaluate_risk(transfers, self.config)
        findings = {
            (item.account_refs, item.transaction_refs)
            for assessment in assessments
            for item in assessment.findings
            if item.rule_id == "R002"
        }

        expected = {
            (("ACC-1", "ACC-2"), ("AB-1", "AB-2", "AB-3")),
            (("ACC-2", "ACC-1"), ("BA-1", "BA-2", "BA-3")),
            (("ACC-1", "ACC-3"), ("AC-1", "AC-2", "AC-3")),
            (("ACC-3", "ACC-2"), ("CB-1", "CB-2", "CB-3")),
        }
        self.assertEqual(findings, expected)

    def test_r002_ignores_noncompleted_and_nontransfer_transactions(self):
        transactions = (
            self._transfer("COMPLETE-1", 0),
            self._transfer("COMPLETE-2", 30),
            self._transfer("PENDING", 60, status="pending"),
            self._transfer("FAILED", 90, status="failed"),
            self._transfer("REVERSED", 120, status="reversed"),
            self._transfer("CARD", 150, transaction_type="card_payment"),
        )
        self.assertFalse(
            any(
                finding.rule_id == "R002"
                for assessment in evaluate_risk(transactions, self.config)
                for finding in assessment.findings
            )
        )

    def test_r002_counts_transfers_with_mixed_currencies_without_aggregation(self):
        transfers = (
            self._transfer("INR", 0, currency="INR"),
            self._transfer("USD", 30, currency="USD"),
            self._transfer("EUR", 60, currency="EUR"),
        )
        finding = next(
            item
            for item in self._assessment(transfers[0], transactions=transfers).findings
            if item.rule_id == "R002"
        )

        self.assertEqual(finding.transaction_refs, ("INR", "USD", "EUR"))
        self.assertEqual(
            tuple(item.currency for item in finding.transactions),
            ("INR", "USD", "EUR"),
        )

    def test_r002_emits_deterministic_separate_qualifying_windows(self):
        transfers = (
            self._transfer("WINDOW-1A", 0),
            self._transfer("WINDOW-1B", 60),
            self._transfer("WINDOW-1C", 120),
            self._transfer("WINDOW-2A", 1_200),
            self._transfer("WINDOW-2B", 1_260),
            self._transfer("WINDOW-2C", 1_320),
        )
        forward = evaluate_risk(transfers, self.config)
        reverse = evaluate_risk(tuple(reversed(transfers)), self.config)
        self.assertEqual(forward, reverse)

        findings = {
            item.transaction_ref: item.findings[0]
            for item in forward
            if item.transaction_ref in {"WINDOW-1A", "WINDOW-2A"}
        }
        self.assertEqual(
            findings["WINDOW-1A"].transaction_refs,
            ("WINDOW-1A", "WINDOW-1B", "WINDOW-1C"),
        )
        self.assertEqual(
            findings["WINDOW-2A"].transaction_refs,
            ("WINDOW-2A", "WINDOW-2B", "WINDOW-2C"),
        )
        self.assertEqual(
            findings["WINDOW-1A"].rule_id,
            "R002",
        )
        self.assertEqual(findings["WINDOW-1A"].account_refs, ("ACC-1", "ACC-2"))

    def test_r002_finds_group_after_earlier_anchor_and_excludes_outside_transfer(self):
        transfers = (
            self._transfer("AT-1000", 0),
            self._transfer("AT-1009", 9 * 60),
            self._transfer("AT-1011", 11 * 60),
            self._transfer("AT-1012", 12 * 60),
        )
        config = replace(
            self.config,
            repeated_transfer_window_seconds=10 * 60,
            minimum_repeated_transfer_count=3,
        )
        forward = evaluate_risk(transfers, config)
        reverse = evaluate_risk(tuple(reversed(transfers)), config)

        self.assertEqual(forward, reverse)
        assessment_by_ref = {item.transaction_ref: item for item in forward}
        self.assertFalse(
            any(
                finding.rule_id == "R002"
                for finding in assessment_by_ref["AT-1000"].findings
            )
        )
        expected_refs = ("AT-1009", "AT-1011", "AT-1012")
        for ref in expected_refs:
            finding = next(
                finding
                for finding in assessment_by_ref[ref].findings
                if finding.rule_id == "R002"
            )
            self.assertEqual(finding.transaction_refs, expected_refs)
            self.assertNotIn("AT-1000", finding.transaction_refs)

    def test_r002_overlapping_maximal_windows_are_distinct_and_score_once(self):
        transfers = (
            self._transfer("OVERLAP-1", 0),
            self._transfer("OVERLAP-2", 5 * 60),
            self._transfer("OVERLAP-3", 10 * 60),
            self._transfer("OVERLAP-4", 15 * 60),
        )
        config = replace(
            self.config,
            repeated_transfer_window_seconds=10 * 60,
            minimum_repeated_transfer_count=3,
        )
        assessments = {
            item.transaction_ref: item
            for item in evaluate_risk(transfers, config)
        }
        expected_groups = (
            ("OVERLAP-1", "OVERLAP-2", "OVERLAP-3"),
            ("OVERLAP-2", "OVERLAP-3", "OVERLAP-4"),
        )
        for ref in ("OVERLAP-2", "OVERLAP-3"):
            findings = tuple(
                finding
                for finding in assessments[ref].findings
                if finding.rule_id == "R002"
            )
            self.assertEqual(
                tuple(finding.transaction_refs for finding in findings),
                expected_groups,
            )
            self.assertEqual(assessments[ref].score, 25)
            self.assertEqual(
                sum(item.applied_points for item in assessments[ref].contributions),
                25,
            )

    def test_r003_triggers_at_minimum_transfer_and_recipient_counts(self):
        transfers = (
            self._transfer("VEL-1", 0, destination="RECIPIENT-1"),
            self._transfer("VEL-2", 30, destination="RECIPIENT-2"),
            self._transfer("VEL-3", 60, destination="RECIPIENT-3"),
            self._transfer("VEL-4", 90, destination="RECIPIENT-1"),
            self._transfer("VEL-5", 120, destination="RECIPIENT-2"),
        )
        finding = next(
            item
            for item in self._assessment(
                transfers[0],
                transactions=transfers,
            ).findings
            if item.rule_id == "R003"
        )

        self.assertEqual(
            finding.transaction_refs,
            tuple(item.transaction_ref for item in transfers),
        )
        self.assertEqual(
            finding.account_refs,
            ("ACC-1", "RECIPIENT-1", "RECIPIENT-2", "RECIPIENT-3"),
        )
        self.assertIn(EvidenceMetric("observed_count", 5), finding.observed)
        self.assertIn(EvidenceMetric("distinct_recipient_count", 3), finding.observed)

    def test_r003_requires_both_minimum_counts(self):
        four_transfers = tuple(
            self._transfer(
                f"FOUR-{index}",
                index * 30,
                destination=f"RECIPIENT-{index % 3}",
            )
            for index in range(4)
        )
        two_recipients = tuple(
            self._transfer(
                f"TWO-RECIPIENTS-{index}",
                index * 30,
                destination=f"RECIPIENT-{index % 2}",
            )
            for index in range(5)
        )
        for transfers in (four_transfers, two_recipients):
            with self.subTest(transfers=transfers):
                self.assertFalse(
                    any(
                        finding.rule_id == "R003"
                        for assessment in evaluate_risk(transfers, self.config)
                        for finding in assessment.findings
                    )
                )

    def test_r003_more_than_minimum_recipients_and_repeated_single_recipient(self):
        four_recipients = tuple(
            self._transfer(
                f"MANY-{index}",
                index * 30,
                destination=f"RECIPIENT-{index}",
            )
            for index in range(5)
        )
        single_recipient = tuple(
            self._transfer(
                f"SINGLE-{index}",
                index * 30,
                destination="RECIPIENT-1",
            )
            for index in range(5)
        )

        finding = next(
            item
            for item in self._assessment(
                four_recipients[0],
                transactions=four_recipients,
            ).findings
            if item.rule_id == "R003"
        )
        self.assertIn(EvidenceMetric("distinct_recipient_count", 5), finding.observed)
        self.assertFalse(
            any(
                item.rule_id == "R003"
                for assessment in evaluate_risk(single_recipient, self.config)
                for item in assessment.findings
            )
        )

    def test_r003_groups_by_source_and_not_recipient_or_currency(self):
        transfers = (
            self._transfer("SOURCE-A-1", 0, source="SOURCE-A", destination="A"),
            self._transfer("SOURCE-A-2", 30, source="SOURCE-A", destination="B"),
            self._transfer("SOURCE-A-3", 60, source="SOURCE-A", destination="C"),
            self._transfer("SOURCE-A-4", 90, source="SOURCE-A", destination="D"),
            self._transfer("SOURCE-A-5", 120, source="SOURCE-A", destination="E"),
            self._transfer("SOURCE-B-1", 0, source="SOURCE-B", destination="A"),
            self._transfer("SOURCE-B-2", 30, source="SOURCE-B", destination="B"),
            self._transfer("SOURCE-B-3", 60, source="SOURCE-B", destination="C"),
            self._transfer("SOURCE-B-4", 90, source="SOURCE-B", destination="D"),
            self._transfer("SOURCE-B-5", 120, source="SOURCE-B", destination="E"),
            self._transfer("MIXED-1", 150, source="SOURCE-C", destination="A", currency="INR"),
            self._transfer("MIXED-2", 180, source="SOURCE-C", destination="B", currency="USD"),
            self._transfer("MIXED-3", 210, source="SOURCE-C", destination="C", currency="EUR"),
            self._transfer("MIXED-4", 240, source="SOURCE-C", destination="D", currency="INR"),
            self._transfer("MIXED-5", 270, source="SOURCE-C", destination="E", currency="USD"),
        )
        assessments = evaluate_risk(transfers, self.config)
        findings = {
            (item.account_refs[0], item.transaction_refs)
            for assessment in assessments
            for item in assessment.findings
            if item.rule_id == "R003"
        }

        self.assertEqual(
            findings,
            {
                ("SOURCE-A", tuple(f"SOURCE-A-{index}" for index in range(1, 6))),
                ("SOURCE-B", tuple(f"SOURCE-B-{index}" for index in range(1, 6))),
                ("SOURCE-C", tuple(f"MIXED-{index}" for index in range(1, 6))),
            },
        )

    def test_r003_ignores_ineligible_statuses_and_nontransfer_types(self):
        eligible = tuple(
            self._transfer(
                f"ELIGIBLE-{index}",
                index * 30,
                destination=f"RECIPIENT-{index}",
            )
            for index in range(1, 4)
        )
        ineligible = (
            self._transfer("PENDING-V", 90, destination="RECIPIENT-4", status="pending"),
            self._transfer("FAILED-V", 120, destination="RECIPIENT-5", status="failed"),
            self._transfer("REVERSED-V", 150, destination="RECIPIENT-6", status="reversed"),
            self._transfer(
                "CARD-V",
                180,
                destination="RECIPIENT-7",
                transaction_type="card_payment",
            ),
        )
        self.assertFalse(
            any(
                item.rule_id == "R003"
                for assessment in evaluate_risk((*eligible, *ineligible), self.config)
                for item in assessment.findings
            )
        )

    def test_r003_includes_exact_boundary_and_excludes_just_outside(self):
        on_boundary = (
            self._transfer("EDGE-1", 0, destination="RECIPIENT-1"),
            self._transfer("EDGE-2", 60, destination="RECIPIENT-2"),
            self._transfer("EDGE-3", 120, destination="RECIPIENT-3"),
            self._transfer("EDGE-4", 240, destination="RECIPIENT-1"),
            self._transfer("EDGE-5", 300, destination="RECIPIENT-2"),
        )
        outside = (
            self._transfer("OUT-1", 0, destination="RECIPIENT-1"),
            self._transfer("OUT-2", 60, destination="RECIPIENT-2"),
            self._transfer("OUT-3", 120, destination="RECIPIENT-3"),
            self._transfer("OUT-4", 240, destination="RECIPIENT-1"),
            self._transfer("OUT-5", 301, destination="RECIPIENT-2"),
        )
        finding = next(
            item
            for item in self._assessment(on_boundary[0], transactions=on_boundary).findings
            if item.rule_id == "R003"
        )
        self.assertEqual(finding.transaction_refs, tuple(item.transaction_ref for item in on_boundary))
        self.assertIn(EvidenceMetric("window_boundary", "inclusive"), finding.threshold)
        self.assertFalse(
            any(
                item.rule_id == "R003"
                for assessment in evaluate_risk(outside, self.config)
                for item in assessment.findings
            )
        )

    def test_r003_is_order_invariant_and_evidence_is_deterministic(self):
        transfers = (
            self._transfer("ORDER-1", 0, destination="RECIPIENT-1"),
            self._transfer("ORDER-2", 60, destination="RECIPIENT-2"),
            self._transfer("ORDER-3", 120, destination="RECIPIENT-3"),
            self._transfer("ORDER-4", 180, destination="RECIPIENT-4"),
            self._transfer("ORDER-5", 240, destination="RECIPIENT-5"),
        )
        forward = evaluate_risk(transfers, self.config)
        reverse = evaluate_risk(tuple(reversed(transfers)), self.config)
        self.assertEqual(forward, reverse)
        finding = next(
            item for item in forward[0].findings if item.rule_id == "R003"
        )
        self.assertEqual(
            finding.transaction_refs,
            tuple(item.transaction_ref for item in finding.transactions),
        )
        self.assertEqual(finding.account_refs, (
            "ACC-1",
            "RECIPIENT-1",
            "RECIPIENT-2",
            "RECIPIENT-3",
            "RECIPIENT-4",
            "RECIPIENT-5",
        ))

    def test_r003_emits_multiple_overlapping_maximal_windows(self):
        transfers = (
            self._transfer("WIN-1", 0, destination="RECIPIENT-1"),
            self._transfer("WIN-2", 60, destination="RECIPIENT-2"),
            self._transfer("WIN-3", 120, destination="RECIPIENT-3"),
            self._transfer("WIN-4", 240, destination="RECIPIENT-4"),
            self._transfer("WIN-5", 300, destination="RECIPIENT-5"),
            self._transfer("WIN-6", 360, destination="RECIPIENT-6"),
        )
        assessments = {
            item.transaction_ref: item
            for item in evaluate_risk(transfers, self.config)
        }
        expected_windows = (
            ("WIN-1", "WIN-2", "WIN-3", "WIN-4", "WIN-5"),
            ("WIN-2", "WIN-3", "WIN-4", "WIN-5", "WIN-6"),
        )
        for ref in ("WIN-2", "WIN-3", "WIN-4", "WIN-5"):
            findings = tuple(
                item.transaction_refs
                for item in assessments[ref].findings
                if item.rule_id == "R003"
            )
            self.assertEqual(findings, expected_windows)
            self.assertEqual(assessments[ref].score, 20)
            self.assertEqual(
                sum(item.applied_points for item in assessments[ref].contributions),
                20,
            )

    def test_r003_deduplicates_scoring_with_r002(self):
        transfers = (
            self._transfer("BOTH-1", 0, destination="RECIPIENT-1"),
            self._transfer("BOTH-2", 30, destination="RECIPIENT-1"),
            self._transfer("BOTH-3", 60, destination="RECIPIENT-1"),
            self._transfer("BOTH-4", 90, destination="RECIPIENT-2"),
            self._transfer("BOTH-5", 120, destination="RECIPIENT-3"),
        )
        assessment = self._assessment(transfers[0], transactions=transfers)
        self.assertEqual(
            {item.rule_id for item in assessment.findings},
            {"R002", "R003"},
        )
        contribution_by_rule = {
            item.rule_id: item for item in assessment.contributions
        }
        self.assertEqual(contribution_by_rule["R002"].configured_points, 25)
        self.assertEqual(contribution_by_rule["R002"].applied_points, 25)
        self.assertEqual(contribution_by_rule["R003"].configured_points, 20)
        self.assertEqual(contribution_by_rule["R003"].applied_points, 0)
        self.assertEqual(contribution_by_rule["R003"].deduplicated_by, "R002")
        self.assertEqual(assessment.score, 25)

    def test_r003_configuration_and_input_validation(self):
        invalid_configurations = (
            {"transfer_velocity_window_seconds": 0},
            {"transfer_velocity_window_seconds": 1.5},
            {"transfer_velocity_window_seconds": True},
            {"minimum_velocity_transfer_count": 0},
            {"minimum_velocity_transfer_count": 2.5},
            {"minimum_velocity_transfer_count": True},
            {"minimum_velocity_distinct_recipient_count": 0},
            {"minimum_velocity_distinct_recipient_count": 2.5},
            {"minimum_velocity_distinct_recipient_count": True},
        )
        for updates in invalid_configurations:
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    replace(self.config, **updates)

        valid = self._transfer("VALID-INPUT", 0, destination="RECIPIENT")
        with self.assertRaises(ValueError):
            evaluate_risk(
                (valid, replace(valid, occurred_at=datetime(2026, 1, 31, 12))),
                self.config,
            )
        with self.assertRaises(ValueError):
            evaluate_risk((valid, replace(valid, transaction_ref="VALID-INPUT")), self.config)

    def test_r003_evidence_does_not_make_currency_aggregations_or_claim_fraud(self):
        transfers = (
            self._transfer("EVIDENCE-1", 0, destination="RECIPIENT-1", currency="INR"),
            self._transfer("EVIDENCE-2", 30, destination="RECIPIENT-2", currency="USD"),
            self._transfer("EVIDENCE-3", 60, destination="RECIPIENT-3", currency="EUR"),
            self._transfer("EVIDENCE-4", 90, destination="RECIPIENT-4", currency="INR"),
            self._transfer("EVIDENCE-5", 120, destination="RECIPIENT-5", currency="USD"),
        )
        finding = next(
            item
            for item in self._assessment(transfers[0], transactions=transfers).findings
            if item.rule_id == "R003"
        )
        metric_names = {item.name for item in (*finding.observed, *finding.threshold)}
        self.assertNotIn("total_amount_minor_units", metric_names)
        self.assertNotIn("fraud", finding.explanation.lower())
        self.assertNotIn("money laundering", finding.explanation.lower())
        self.assertTrue(
            all(item.occurred_at.tzinfo == timezone.utc for item in finding.transactions)
        )

    def test_r004_detects_valid_three_account_and_longer_paths(self):
        three_account_path = (
            self._transfer("PATH-3A", 0, source="ACC-A", destination="ACC-B"),
            self._transfer("PATH-3B", 8 * 60, source="ACC-B", destination="ACC-C"),
        )
        longer_path = (
            self._transfer("PATH-L1", 0, source="ACC-D", destination="ACC-E"),
            self._transfer("PATH-L2", 8 * 60, source="ACC-E", destination="ACC-F"),
            self._transfer("PATH-L3", 15 * 60, source="ACC-F", destination="ACC-G"),
        )

        for path, refs, accounts in (
            (
                three_account_path,
                ("PATH-3A", "PATH-3B"),
                ("ACC-A", "ACC-B", "ACC-C"),
            ),
            (
                longer_path,
                ("PATH-L1", "PATH-L2", "PATH-L3"),
                ("ACC-D", "ACC-E", "ACC-F", "ACC-G"),
            ),
        ):
            with self.subTest(path=refs):
                assessment = self._assessment(path[0], transactions=path)
                finding = next(
                    item for item in assessment.findings if item.rule_id == "R004"
                )
                self.assertEqual(finding.transaction_refs, refs)
                self.assertEqual(finding.account_refs, accounts)
                self.assertIn(EvidenceMetric("path_length", len(path)), finding.observed)

    def test_r004_reports_disconnected_paths_separately_and_suppresses_subpaths(self):
        paths = (
            self._transfer("DISC-1", 0, source="A", destination="B"),
            self._transfer("DISC-2", 60, source="B", destination="C"),
            self._transfer("DISC-3", 600, source="X", destination="Y"),
            self._transfer("DISC-4", 660, source="Y", destination="Z"),
            self._transfer("DISC-5", 720, source="Z", destination="W"),
        )
        assessments = evaluate_risk(paths, self.config)
        findings = {
            (item.account_refs, item.transaction_refs)
            for assessment in assessments
            for item in assessment.findings
            if item.rule_id == "R004"
        }
        self.assertEqual(
            findings,
            {
                (("A", "B", "C"), ("DISC-1", "DISC-2")),
                (("X", "Y", "Z", "W"), ("DISC-3", "DISC-4", "DISC-5")),
            },
        )

    def test_r004_requires_connected_edges_and_preserves_direction(self):
        not_connected = (
            self._transfer("NCON-1", 0, source="A", destination="B"),
            self._transfer("NCON-2", 60, source="C", destination="B"),
            self._transfer("NCON-3", 120, source="D", destination="C"),
        )
        reverse_only = (
            self._transfer("REV-1", 0, source="A", destination="B"),
            self._transfer("REV-2", 60, source="C", destination="B"),
            self._transfer("REV-3", 120, source="C", destination="D"),
        )
        for transactions in (not_connected, reverse_only):
            with self.subTest(transactions=transactions):
                self.assertFalse(
                    any(
                        finding.rule_id == "R004"
                        for assessment in evaluate_risk(transactions, self.config)
                        for finding in assessment.findings
                    )
                )

    def test_r004_allows_cycles_and_repeated_accounts_in_a_path(self):
        cycle = (
            self._transfer("CYCLE-1", 0, source="A", destination="B"),
            self._transfer("CYCLE-2", 60, source="B", destination="C"),
            self._transfer("CYCLE-3", 120, source="C", destination="A"),
            self._transfer("CYCLE-4", 180, source="A", destination="D"),
        )
        finding = next(
            item
            for item in self._assessment(cycle[0], transactions=cycle).findings
            if item.rule_id == "R004"
        )
        self.assertEqual(
            finding.transaction_refs,
            ("CYCLE-1", "CYCLE-2", "CYCLE-3", "CYCLE-4"),
        )
        self.assertEqual(finding.account_refs, ("A", "B", "C", "A", "D"))
        self.assertIn(EvidenceMetric("distinct_account_count", 4), finding.observed)

    def test_r004_includes_exact_window_boundary_and_excludes_just_outside(self):
        exact = (
            self._transfer("EXACT-1", 0, source="A", destination="B"),
            self._transfer("EXACT-2", 1_800, source="B", destination="C"),
        )
        outside = (
            self._transfer("OUTSIDE-CHAIN-1", 0, source="A", destination="B"),
            self._transfer("OUTSIDE-CHAIN-2", 1_801, source="B", destination="C"),
        )
        finding = next(
            item
            for item in self._assessment(exact[0], transactions=exact).findings
            if item.rule_id == "R004"
        )
        self.assertEqual(
            finding.transaction_refs,
            tuple(item.transaction_ref for item in exact),
        )
        self.assertIn(EvidenceMetric("window_boundary", "inclusive"), finding.threshold)
        self.assertFalse(
            any(
                item.rule_id == "R004"
                for assessment in evaluate_risk(outside, self.config)
                for item in assessment.findings
            )
        )

    def test_r004_requires_strictly_increasing_timestamps(self):
        equal_timestamps = (
            self._transfer("EQUAL-1", 0, source="A", destination="B"),
            self._transfer("EQUAL-2", 0, source="B", destination="C"),
            self._transfer("EQUAL-3", 60, source="C", destination="D"),
        )
        findings = tuple(
            finding
            for assessment in evaluate_risk(equal_timestamps, self.config)
            for finding in assessment.findings
            if finding.rule_id == "R004"
        )
        self.assertTrue(findings)
        self.assertTrue(
            all(
                finding.transaction_refs == ("EQUAL-2", "EQUAL-3")
                for finding in findings
            )
        )

    def test_r004_ignores_noncompleted_and_nontransfer_transactions(self):
        eligible = (self._transfer("ELIGIBLE-1", 0, source="A", destination="B"),)
        ineligible = (
            self._transfer("PENDING-CHAIN", 90, source="C", destination="D", status="pending"),
            self._transfer("FAILED-CHAIN", 120, source="C", destination="D", status="failed"),
            self._transfer("REVERSED-CHAIN", 150, source="C", destination="D", status="reversed"),
            self._transfer(
                "NONTRANSFER-CHAIN",
                180,
                source="C",
                destination="D",
                transaction_type="card_payment",
            ),
        )
        self.assertFalse(
            any(
                item.rule_id == "R004"
                for assessment in evaluate_risk((*eligible, *ineligible), self.config)
                for item in assessment.findings
            )
        )

    def test_r004_retains_distinct_branching_paths_deterministically(self):
        graph = (
            self._transfer("BRANCH-1", 0, source="A", destination="B"),
            self._transfer("BRANCH-2", 60, source="B", destination="C"),
            self._transfer("BRANCH-3", 90, source="B", destination="D"),
            self._transfer("BRANCH-4", 120, source="C", destination="E"),
            self._transfer("BRANCH-5", 150, source="D", destination="F"),
        )
        forward = evaluate_risk(graph, self.config)
        shuffled = evaluate_risk((graph[3], graph[0], graph[4], graph[2], graph[1]), self.config)
        self.assertEqual(forward, shuffled)
        findings = {
            item.transaction_refs
            for assessment in forward
            for item in assessment.findings
            if item.rule_id == "R004"
        }
        self.assertEqual(
            findings,
            {
                ("BRANCH-1", "BRANCH-2", "BRANCH-4"),
                ("BRANCH-1", "BRANCH-3", "BRANCH-5"),
            },
        )

    def test_r004_evidence_is_complete_and_amount_currency_independent(self):
        path = (
            self._transfer("EVIDENCE-CHAIN-1", 0, source="A", destination="B", currency="INR"),
            self._transfer("EVIDENCE-CHAIN-2", 10 * 60, source="B", destination="C", currency="USD"),
            self._transfer("EVIDENCE-CHAIN-3", 20 * 60, source="C", destination="D", currency="EUR"),
        )
        finding = next(
            item
            for item in self._assessment(path[0], transactions=path).findings
            if item.rule_id == "R004"
        )
        self.assertEqual(finding.rule_name, "Multi-account transfer chain")
        self.assertEqual(finding.transaction_refs, tuple(item.transaction_ref for item in path))
        self.assertEqual(finding.account_refs, ("A", "B", "C", "D"))
        self.assertEqual(
            tuple(item.occurred_at for item in finding.transactions),
            tuple(item.occurred_at for item in path),
        )
        self.assertIn(EvidenceMetric("path_length", 3), finding.observed)
        self.assertIn(EvidenceMetric("distinct_account_count", 4), finding.observed)
        self.assertIn(EvidenceMetric("window_seconds", 1_800), finding.threshold)
        self.assertIn(EvidenceMetric("minimum_transfer_count", 2), finding.threshold)
        self.assertIn(EvidenceMetric("minimum_distinct_account_count", 3), finding.threshold)
        self.assertNotIn("amount_minor_units", {item.name for item in finding.observed})
        self.assertIn("A", finding.explanation)
        self.assertNotIn("fraud", finding.explanation.lower())

    def test_r004_deduplicates_transfer_pattern_scoring_with_r002_and_r003(self):
        graph = (
            self._transfer("OVERLAP-AB1", 0, source="A", destination="B"),
            self._transfer("OVERLAP-BA1", 10, source="B", destination="A"),
            self._transfer("OVERLAP-AB2", 20, source="A", destination="B"),
            self._transfer("OVERLAP-BA2", 30, source="B", destination="A"),
            self._transfer("OVERLAP-AB3", 40, source="A", destination="B"),
            self._transfer("OVERLAP-BA3", 50, source="B", destination="A"),
            self._transfer("OVERLAP-AC", 60, source="A", destination="C"),
            self._transfer("OVERLAP-CA", 70, source="C", destination="A"),
            self._transfer("OVERLAP-AD", 80, source="A", destination="D"),
            self._transfer("OVERLAP-DA", 90, source="D", destination="A"),
            self._transfer("OVERLAP-AE", 100, source="A", destination="E"),
        )
        assessment = self._assessment(graph[0], transactions=graph)
        self.assertTrue(
            {"R002", "R003", "R004"}.issubset(
                {item.rule_id for item in assessment.findings}
            )
        )
        self.assertEqual(assessment.score, 30)
        self.assertEqual(
            sum(item.applied_points for item in assessment.contributions),
            30,
        )
        applied = [
            item for item in assessment.contributions if item.applied_points > 0
        ]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0].rule_id, "R004")

    def test_r004_rejects_duplicate_references_and_invalid_timestamps(self):
        valid = self._transfer("DUP-CHAIN", 0, source="A", destination="B")
        duplicate = replace(valid, occurred_at=valid.occurred_at + timedelta(seconds=1))
        with self.assertRaises(ValueError):
            evaluate_risk((valid, duplicate), self.config)
        with self.assertRaises(ValueError):
            evaluate_risk(
                (
                    valid,
                    replace(
                        valid,
                        transaction_ref="NAIVE-CHAIN",
                        occurred_at=datetime(2026, 1, 31, 12),
                    ),
                ),
                self.config,
            )

    def test_r004_configuration_validation_and_empty_input(self):
        self.assertEqual(evaluate_risk((), self.config), ())
        invalid = (
            {"transfer_chain_window_seconds": 0},
            {"transfer_chain_window_seconds": 1.5},
            {"minimum_transfer_chain_length": 1},
            {"minimum_transfer_chain_length": True},
            {"minimum_transfer_chain_account_count": 2},
            {"minimum_transfer_chain_account_count": 2.5},
        )
        for updates in invalid:
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    replace(self.config, **updates)

    def test_r002_evidence_is_complete_and_utc_normalized(self):
        local_time = datetime(
            2026,
            1,
            31,
            17,
            tzinfo=timezone(timedelta(hours=5)),
        )
        transfers = tuple(
            replace(
                self._transfer(f"EVIDENCE-{index}", index * 30),
                occurred_at=local_time + timedelta(seconds=index * 30),
            )
            for index in range(3)
        )
        finding = next(
            item
            for item in self._assessment(transfers[0], transactions=transfers).findings
            if item.rule_id == "R002"
        )
        self.assertEqual(finding.rule_name, "Repeated transfers between the same accounts")
        self.assertEqual(finding.account_refs, ("ACC-1", "ACC-2"))
        self.assertEqual(
            tuple(item.transaction_ref for item in finding.transactions),
            finding.transaction_refs,
        )
        self.assertTrue(
            all(item.occurred_at.tzinfo == timezone.utc for item in finding.transactions)
        )
        self.assertIn(EvidenceMetric("observed_count", 3), finding.observed)
        self.assertIn(EvidenceMetric("minimum_transaction_count", 3), finding.threshold)
        self.assertIn(EvidenceMetric("window_seconds", 600), finding.threshold)
        self.assertIn("3 completed transfers", finding.explanation)
        self.assertNotIn("fraud", finding.explanation.lower())
        self.assertNotIn("money laundering", finding.explanation.lower())

    def test_r002_has_its_own_score_family_and_preserves_overlapping_r001(self):
        config = replace(
            self.config,
            contributions=(
                RuleContributionConfig("R001", 25, "amount_anomaly"),
                RuleContributionConfig("R002", 30, "transfer_pattern"),
                RuleContributionConfig("R003", 20, "transfer_pattern"),
                RuleContributionConfig("R004", 30, "transfer_pattern"),
                RuleContributionConfig("R006", 20, "amount_anomaly"),
            ),
        )
        transfers = (
            self._transfer("SCORE-1", 0, amount=1_001),
            self._transfer("SCORE-2", 30, amount=1_001),
            self._transfer("SCORE-3", 60, amount=1_001),
        )
        assessment = self._assessment(
            transfers[0],
            config=config,
            transactions=transfers,
        )
        self.assertEqual({item.rule_id for item in assessment.findings}, {"R001", "R002"})
        self.assertEqual(assessment.score, 55)
        contributions = {item.rule_id: item for item in assessment.contributions}
        self.assertEqual(contributions["R001"].applied_points, 25)
        self.assertEqual(contributions["R002"].configured_points, 30)
        self.assertEqual(contributions["R002"].applied_points, 30)

    def test_r002_configuration_requires_positive_integer_window_and_minimum(self):
        for field_name, invalid in (
            ("repeated_transfer_window_seconds", 0),
            ("repeated_transfer_window_seconds", -1),
            ("repeated_transfer_window_seconds", 1.5),
            ("repeated_transfer_window_seconds", True),
            ("minimum_repeated_transfer_count", 0),
            ("minimum_repeated_transfer_count", -1),
            ("minimum_repeated_transfer_count", 2.5),
            ("minimum_repeated_transfer_count", True),
        ):
            with self.subTest(field=field_name, invalid=invalid):
                with self.assertRaises(ValueError):
                    replace(self.config, **{field_name: invalid})

    def test_r002_rejects_duplicate_references_naive_timestamps_and_bad_pairs(self):
        duplicate = (
            self._transfer("DUPLICATE", 0),
            self._transfer("DUPLICATE", 30),
        )
        with self.assertRaises(ValueError):
            evaluate_risk(duplicate, self.config)
        with self.assertRaises(ValueError):
            evaluate_risk(
                (self._transfer("NAIVE", 0), replace(
                    self._transfer("NAIVE", 30),
                    transaction_ref="NAIVE-2",
                    occurred_at=datetime(2026, 1, 31, 12),
                )),
                self.config,
            )
        for invalid_counterparty in ("", "  ", "ACC-1"):
            with self.subTest(counterparty=invalid_counterparty):
                invalid = self._transfer(
                    "INVALID-PAIR",
                    0,
                    destination=invalid_counterparty,
                )
                with self.assertRaises(ValueError):
                    evaluate_risk((invalid,), self.config)
        missing_destination = self._transaction(
            ref="MISSING-DESTINATION",
            transaction_type="transfer",
        )
        with self.assertRaises(ValueError):
            evaluate_risk((missing_destination,), self.config)

    def test_invalid_configuration_and_inputs_are_rejected(self):
        invalid_config_factories = (
            lambda: replace(
                self.config,
                currency_thresholds=(
                    CurrencyThreshold("INR", -1, 100),
                    CurrencyThreshold("USD", 2_000, 200),
                    CurrencyThreshold("EUR", 3_000, 300),
                ),
            ),
            lambda: replace(self.config, history_window_seconds=0),
            lambda: replace(self.config, minimum_history_count=0),
            lambda: replace(self.config, historical_multiplier=0),
            lambda: replace(
                self.config,
                bands=(RiskBand("low", 0, 25), RiskBand("high", 25, 100)),
            ),
            lambda: replace(
                self.config,
                contributions=(
                    RuleContributionConfig("R001", 25, "amount"),
                    RuleContributionConfig("R002", 25, "transfer_pattern"),
                    RuleContributionConfig("R003", 20, "transfer_pattern"),
                    RuleContributionConfig("R004", 30, "transfer_pattern"),
                    RuleContributionConfig("R006", 20, "history"),
                ),
            ),
        )
        for build_config in invalid_config_factories:
            with self.subTest(build_config=build_config):
                with self.assertRaises(ValueError):
                    evaluate_risk((), build_config())

        invalid_config_factories = (
            lambda: replace(
                self.config,
                currency_thresholds=(
                    CurrencyThreshold("inr", 1_000, 100),
                    CurrencyThreshold("USD", 2_000, 200),
                    CurrencyThreshold("EUR", 3_000, 300),
                ),
            ),
            lambda: replace(
                self.config,
                currency_thresholds=(
                    CurrencyThreshold("INR", 1_000, 100),
                    CurrencyThreshold("INR", 2_000, 200),
                    CurrencyThreshold("EUR", 3_000, 300),
                ),
            ),
            lambda: replace(
                self.config,
                currency_thresholds=(
                    CurrencyThreshold("INR", 1.5, 100),
                    CurrencyThreshold("USD", 2_000, 200),
                    CurrencyThreshold("EUR", 3_000, 300),
                ),
            ),
            lambda: replace(
                self.config,
                currency_thresholds=(
                    CurrencyThreshold("INR", 1_000, -1),
                    CurrencyThreshold("USD", 2_000, 200),
                    CurrencyThreshold("EUR", 3_000, 300),
                ),
            ),
            lambda: replace(
                self.config,
                currency_thresholds=(
                    CurrencyThreshold("INR", 1_000, 100),
                    CurrencyThreshold("USD", 2_000, 200),
                    CurrencyThreshold("EUR", 3_000, 300),
                    CurrencyThreshold("GBP", 4_000, 400),
                ),
            ),
            lambda: replace(
                self.config,
                contributions=(
                    RuleContributionConfig("R001", 25, "amount_anomaly"),
                    RuleContributionConfig("R002", 25, "transfer_pattern"),
                    RuleContributionConfig("R003", 20, "transfer_pattern"),
                    RuleContributionConfig("R004", 30, "transfer_pattern"),
                    RuleContributionConfig("R001", 20, "amount_anomaly"),
                ),
            ),
            lambda: replace(
                self.config,
                contributions=(
                    RuleContributionConfig("R001", -1, "amount_anomaly"),
                    RuleContributionConfig("R002", 25, "transfer_pattern"),
                    RuleContributionConfig("R003", 20, "transfer_pattern"),
                    RuleContributionConfig("R004", 30, "transfer_pattern"),
                    RuleContributionConfig("R006", 20, "amount_anomaly"),
                ),
            ),
            lambda: replace(
                self.config,
                contributions=(
                    RuleContributionConfig("R001", 1.5, "amount_anomaly"),
                    RuleContributionConfig("R002", 25, "transfer_pattern"),
                    RuleContributionConfig("R003", 20, "transfer_pattern"),
                    RuleContributionConfig("R004", 30, "transfer_pattern"),
                    RuleContributionConfig("R006", 20, "amount_anomaly"),
                ),
            ),
            lambda: replace(
                self.config,
                bands=(
                    RiskBand("low", 0, 24),
                    RiskBand("medium", 26, 49),
                    RiskBand("high", 50, 74),
                    RiskBand("critical", 75, 100),
                ),
            ),
            lambda: replace(
                self.config,
                bands=(
                    RiskBand("low", 0, 24),
                    RiskBand("low", 25, 49),
                    RiskBand("high", 50, 74),
                    RiskBand("critical", 75, 100),
                ),
            ),
            lambda: replace(self.config, history_window_seconds=-1),
            lambda: replace(self.config, history_window_seconds=1.5),
            lambda: replace(self.config, minimum_history_count=True),
            lambda: replace(self.config, historical_multiplier=0),
        )
        for build_config in invalid_config_factories:
            with self.subTest(build_config=build_config):
                with self.assertRaises(ValueError):
                    build_config()

        with self.assertRaises(ValueError):
            evaluate_risk((self._transaction(currency="GBP"),), self.config)
        with self.assertRaises(ValueError):
            evaluate_risk((self._transaction(currency="inr"),), self.config)
        with self.assertRaises(ValueError):
            evaluate_risk((self._transaction(occurred_at=datetime(2026, 1, 1)),))
        with self.assertRaises(ValueError):
            evaluate_risk(
                (
                    self._transaction(ref="DUP"),
                    self._transaction(ref="DUP", amount=200),
                ),
                self.config,
            )
        for invalid_transaction in (
            self._transaction(ref=" "),
            self._transaction(ref=None),
            self._transaction(ref=123),
            self._transaction(status="unknown"),
            self._transaction(status=[]),
            self._transaction(transaction_type="unknown"),
            self._transaction(transaction_type=[]),
            self._transaction(amount=1.5),
            self._transaction(amount=True),
        ):
            with self.subTest(transaction=invalid_transaction):
                with self.assertRaises(ValueError):
                    evaluate_risk((invalid_transaction,), self.config)

    def test_risk_data_structures_are_immutable(self):
        assessment = self._assessment(self._transaction(amount=1_001))
        with self.assertRaises(FrozenInstanceError):
            assessment.score = 10


if __name__ == "__main__":
    unittest.main()
