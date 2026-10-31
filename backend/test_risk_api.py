import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from database import Base, enable_sqlite_foreign_keys
from main import app, get_assessment_limits, get_db
from models import Account, Customer, Transaction
from risk_data_loader import load_risk_data
from risk_engine import RiskConfig
from risk_service import AssessmentLimits


class RiskApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "risk-api-tests.sqlite"
        self.engine = create_engine(
            f"sqlite:///{database_path}",
            connect_args={"check_same_thread": False},
        )
        enable_sqlite_foreign_keys(self.engine)
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self.now = datetime(2026, 10, 1, tzinfo=timezone.utc)

        def override_db():
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_assessment_limits] = lambda: AssessmentLimits()
        self.client = TestClient(app)
        with self.session_factory() as session:
            customer = Customer(
                customer_ref="API-CUSTOMER",
                full_name="Synthetic API Customer",
                country="US",
            )
            session.add(customer)
            session.flush()
            self.accounts = {}
            for account_ref in ("SOURCE", "DEST-1", "DEST-2", "DEST-3", "DEST-4"):
                account = Account(
                    account_ref=account_ref,
                    customer_id=customer.id,
                    account_type="savings",
                    currency="INR",
                    balance_minor_units=987_654,
                )
                session.add(account)
                self.accounts[account_ref] = account
            session.commit()
            self.account_ids = {
                row.account_ref: row.id
                for row in session.scalars(select(Account)).all()
            }

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _transaction(
        self,
        ref,
        *,
        occurred_at=None,
        source="SOURCE",
        destination=None,
        amount=100,
        currency="INR",
        transaction_type="card_payment",
        status="completed",
    ):
        return Transaction(
            transaction_ref=ref,
            account_id=self.account_ids[source],
            counterparty_account_id=(
                self.account_ids[destination] if destination is not None else None
            ),
            amount_minor_units=amount,
            currency=currency,
            transaction_type=transaction_type,
            status=status,
            occurred_at=occurred_at or self.now,
        )

    def _add(self, *transactions):
        with self.session_factory() as session:
            session.add_all(transactions)
            session.commit()

    def _request(self, start=None, end=None):
        return {
            "start_at": (start or self.now).isoformat(),
            "end_at": (end or self.now + timedelta(days=1)).isoformat(),
        }

    def test_source_and_counterparty_account_references_are_mapped(self):
        self._add(
            self._transaction(
                "MAP-TRANSFER",
                destination="DEST-1",
                transaction_type="transfer",
                amount=1_000_001,
            )
        )
        with self.session_factory() as session:
            loaded = load_risk_data(
                session,
                self.now,
                self.now + timedelta(days=1),
                RiskConfig(),
                maximum_rows=100,
            )
        mapped = next(
            item
            for item in loaded.transactions
            if item.transaction_ref == "MAP-TRANSFER"
        )
        self.assertEqual(mapped.account_ref, "SOURCE")
        self.assertEqual(mapped.counterparty_account_ref, "DEST-1")
        response = self.client.post("/risk/assessments", json=self._request())
        self.assertEqual(response.status_code, 200)
        assessment = response.json()["assessments"][0]
        self.assertEqual(
            {
                key: assessment[key]
                for key in (
                    "transaction_ref",
                    "occurred_at",
                    "amount_minor_units",
                    "currency",
                    "transaction_type",
                    "status",
                    "source_account_ref",
                    "counterparty_account_ref",
                )
            },
            {
                "transaction_ref": "MAP-TRANSFER",
                "occurred_at": "2026-10-01T00:00:00Z",
                "amount_minor_units": 1_000_001,
                "currency": "INR",
                "transaction_type": "transfer",
                "status": "completed",
                "source_account_ref": "SOURCE",
                "counterparty_account_ref": "DEST-1",
            },
        )
        finding = next(item for item in assessment["findings"] if item["rule_id"] == "R001")
        self.assertEqual(finding["account_refs"], ["SOURCE"])
        self.assertEqual(finding["transaction_refs"], ["MAP-TRANSFER"])

    def test_nontransfer_maps_optional_counterparty_reference(self):
        self._add(
            self._transaction(
                "CARD-WITH-COUNTERPARTY",
                destination="DEST-1",
                transaction_type="card_payment",
            )
        )
        with self.session_factory() as session:
            loaded = load_risk_data(
                session,
                self.now,
                self.now + timedelta(days=1),
                RiskConfig(),
                maximum_rows=100,
            )
        mapped = next(
            item
            for item in loaded.transactions
            if item.transaction_ref == "CARD-WITH-COUNTERPARTY"
        )
        self.assertEqual(mapped.account_ref, "SOURCE")
        self.assertEqual(mapped.counterparty_account_ref, "DEST-1")
        response = self.client.post("/risk/assessments", json=self._request())
        self.assertEqual(response.status_code, 200)
        assessment = response.json()["assessments"][0]
        self.assertEqual(response.json()["assessment_count"], 1)
        self.assertEqual(assessment["source_account_ref"], "SOURCE")
        self.assertEqual(assessment["counterparty_account_ref"], "DEST-1")

    def test_target_without_counterparty_returns_null_and_no_findings(self):
        self._add(self._transaction("NO-COUNTERPARTY"))
        response = self.client.post("/risk/assessments", json=self._request())
        self.assertEqual(response.status_code, 200)
        assessment = response.json()["assessments"][0]
        self.assertEqual(assessment["transaction_ref"], "NO-COUNTERPARTY")
        self.assertEqual(assessment["occurred_at"], "2026-10-01T00:00:00Z")
        self.assertEqual(assessment["amount_minor_units"], 100)
        self.assertEqual(assessment["currency"], "INR")
        self.assertEqual(assessment["transaction_type"], "card_payment")
        self.assertEqual(assessment["status"], "completed")
        self.assertEqual(assessment["source_account_ref"], "SOURCE")
        self.assertIsNone(assessment["counterparty_account_ref"])
        self.assertEqual(assessment["findings"], [])
        self.assertEqual(assessment["score"], 0)
        self.assertEqual(assessment["risk_band"], "low")

    def test_invalid_stored_transfer_is_reported_as_sanitized_internal_error(self):
        self._add(self._transaction("CORRUPT-BASE", amount=10))
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.exec_driver_sql(
                "INSERT INTO transactions "
                "(transaction_ref, account_id, counterparty_account_id, "
                "amount_minor_units, currency, transaction_type, status, "
                "occurred_at, created_at) "
                "VALUES (?, ?, NULL, ?, ?, 'transfer', 'completed', ?, ?)",
                (
                    "CORRUPT-TRANSFER",
                    self.account_ids["SOURCE"],
                    100,
                    "INR",
                    self.now.isoformat(),
                    self.now.isoformat(),
                ),
            )
            connection.commit()

        response = self.client.post("/risk/assessments", json=self._request())
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(),
            {"detail": "Stored transaction data could not be assessed"},
        )
        self.assertNotIn("CORRUPT-TRANSFER", response.text)
        self.assertNotIn("SELECT", response.text)

    def test_sqlite_naive_utc_timestamps_and_offset_request_are_normalized(self):
        self._add(
            self._transaction(
                "UTC-EVIDENCE",
                occurred_at=self.now,
                amount=1_000_001,
            )
        )
        offset_start = self.now.astimezone(timezone(timedelta(hours=5, minutes=30)))
        offset_end = (self.now + timedelta(days=1)).astimezone(
            timezone(timedelta(hours=5, minutes=30))
        )
        response = self.client.post(
            "/risk/assessments",
            json=self._request(offset_start, offset_end),
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["start_at"], "2026-10-01T00:00:00Z")
        evidence = result["assessments"][0]["findings"][0]["transactions"][0]
        self.assertEqual(evidence["occurred_at"], "2026-10-01T00:00:00Z")

    def test_target_interval_is_half_open(self):
        self._add(
            self._transaction("AT-START", occurred_at=self.now),
            self._transaction(
                "BEFORE-END",
                occurred_at=self.now + timedelta(days=1) - timedelta(microseconds=1),
            ),
            self._transaction("AT-END", occurred_at=self.now + timedelta(days=1)),
        )
        response = self.client.post("/risk/assessments", json=self._request())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {item["transaction_ref"] for item in response.json()["assessments"]},
            {"AT-START", "BEFORE-END"},
        )

    def test_context_outside_target_range_can_support_target_finding(self):
        self._add(
            self._transaction(
                "CTX-1",
                occurred_at=self.now - timedelta(minutes=5),
                destination="DEST-1",
                transaction_type="transfer",
            ),
            self._transaction(
                "TARGET-TRANSFER-1",
                occurred_at=self.now,
                destination="DEST-1",
                transaction_type="transfer",
            ),
            self._transaction(
                "CTX-2",
                occurred_at=self.now + timedelta(minutes=2),
                destination="DEST-1",
                transaction_type="transfer",
            ),
        )
        response = self.client.post(
            "/risk/assessments",
            json=self._request(self.now, self.now + timedelta(minutes=1)),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["transaction_ref"] for item in response.json()["assessments"]],
            ["TARGET-TRANSFER-1"],
        )
        assessment = response.json()["assessments"][0]
        self.assertEqual(assessment["source_account_ref"], "SOURCE")
        self.assertEqual(assessment["counterparty_account_ref"], "DEST-1")
        findings = assessment["findings"]
        r002 = next(item for item in findings if item["rule_id"] == "R002")
        self.assertEqual(
            r002["transaction_refs"],
            ["CTX-1", "TARGET-TRANSFER-1", "CTX-2"],
        )
        self.assertEqual(
            {item["transaction_ref"] for item in assessment["findings"][0]["transactions"]}
            - {"TARGET-TRANSFER-1"},
            {"CTX-1", "CTX-2"},
        )

    def test_r006_receives_complete_lookback_baseline(self):
        historical_rows = [
            self._transaction(
                f"HISTORY-{index:02d}",
                occurred_at=self.now - timedelta(days=1, minutes=index),
                amount=100,
            )
            for index in range(20)
        ]
        target = self._transaction(
            "HISTORY-TARGET",
            occurred_at=self.now,
            amount=400_000,
        )
        self._add(*historical_rows, target)
        response = self.client.post(
            "/risk/assessments",
            json=self._request(self.now, self.now + timedelta(minutes=1)),
        )
        self.assertEqual(response.status_code, 200)
        assessment = response.json()["assessments"][0]
        finding = next(item for item in assessment["findings"] if item["rule_id"] == "R006")
        baseline_count = next(
            metric["value"] for metric in finding["observed"]
            if metric["name"] == "baseline_count"
        )
        self.assertEqual(baseline_count, 20)
        self.assertEqual(len(finding["transaction_refs"]), 21)
        self.assertEqual(assessment["transaction_ref"], "HISTORY-TARGET")

    def test_oversized_range_returns_413(self):
        response = self.client.post(
            "/risk/assessments",
            json=self._request(self.now, self.now + timedelta(days=8)),
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            response.json(),
            {"detail": "requested target range exceeds the configured maximum"},
        )

    def test_context_row_limit_rejects_without_partial_response(self):
        self._add(
            self._transaction("LIMIT-TARGET"),
            self._transaction(
                "LIMIT-HISTORY",
                occurred_at=self.now - timedelta(days=1),
            ),
        )
        app.dependency_overrides[get_assessment_limits] = lambda: AssessmentLimits(
            maximum_target_range_seconds=7 * 24 * 60 * 60,
            maximum_loaded_rows=1,
        )
        response = self.client.post("/risk/assessments", json=self._request())
        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            response.json(),
            {"detail": "complete assessment context exceeds the configured row limit"},
        )

    def test_empty_target_interval_returns_empty_assessments(self):
        response = self.client.post("/risk/assessments", json=self._request())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["assessment_count"], 0)
        self.assertEqual(response.json()["assessments"], [])

    def test_response_contains_findings_contributions_and_utc_evidence(self):
        self._add(
            self._transaction(
                "RESPONSE-TRANSFER",
                occurred_at=self.now,
                destination="DEST-1",
                amount=1_000_001,
                transaction_type="transfer",
            )
        )
        response = self.client.post("/risk/assessments", json=self._request())
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["assessment_count"], 1)
        assessment = payload["assessments"][0]
        self.assertEqual(assessment["transaction_ref"], "RESPONSE-TRANSFER")
        self.assertEqual(assessment["occurred_at"], "2026-10-01T00:00:00Z")
        self.assertEqual(assessment["amount_minor_units"], 1_000_001)
        self.assertEqual(assessment["currency"], "INR")
        self.assertEqual(assessment["transaction_type"], "transfer")
        self.assertEqual(assessment["status"], "completed")
        self.assertEqual(assessment["source_account_ref"], "SOURCE")
        self.assertEqual(assessment["counterparty_account_ref"], "DEST-1")
        self.assertEqual(assessment["score"], 25)
        self.assertEqual(assessment["risk_band"], "medium")
        finding = assessment["findings"][0]
        self.assertEqual(finding["rule_id"], "R001")
        self.assertEqual(finding["configured_points"], 25)
        self.assertEqual(finding["applied_points"], 25)
        self.assertIsNone(finding["deduplicated_by"])
        self.assertEqual(
            finding["transactions"][0]["occurred_at"],
            "2026-10-01T00:00:00Z",
        )
        self.assertEqual(assessment["contributions"][0]["configured_points"], 25)

    def test_assessment_is_read_only_for_transaction_fields_and_balances(self):
        self._add(
            self._transaction(
                "READ-ONLY",
                destination="DEST-1",
                amount=1_000_001,
                transaction_type="transfer",
            )
        )
        with self.session_factory() as session:
            before_transaction = session.scalar(
                select(Transaction).where(Transaction.transaction_ref == "READ-ONLY")
            )
            before = (
                before_transaction.id,
                before_transaction.account_id,
                before_transaction.counterparty_account_id,
                before_transaction.amount_minor_units,
                before_transaction.currency,
                before_transaction.transaction_type,
                before_transaction.status,
                before_transaction.occurred_at,
            )
            balances = tuple(
                session.execute(
                    select(Account.account_ref, Account.balance_minor_units)
                    .order_by(Account.account_ref)
                ).all()
            )
            counts = (
                session.scalar(select(func.count()).select_from(Customer)),
                session.scalar(select(func.count()).select_from(Account)),
                session.scalar(select(func.count()).select_from(Transaction)),
            )

        response = self.client.post("/risk/assessments", json=self._request())
        self.assertEqual(response.status_code, 200)
        with self.session_factory() as session:
            after_transaction = session.scalar(
                select(Transaction).where(Transaction.transaction_ref == "READ-ONLY")
            )
            after = (
                after_transaction.id,
                after_transaction.account_id,
                after_transaction.counterparty_account_id,
                after_transaction.amount_minor_units,
                after_transaction.currency,
                after_transaction.transaction_type,
                after_transaction.status,
                after_transaction.occurred_at,
            )
            self.assertEqual(
                tuple(
                    session.execute(
                        select(Account.account_ref, Account.balance_minor_units)
                        .order_by(Account.account_ref)
                    ).all()
                ),
                balances,
            )
            self.assertEqual(
                (
                    session.scalar(select(func.count()).select_from(Customer)),
                    session.scalar(select(func.count()).select_from(Account)),
                    session.scalar(select(func.count()).select_from(Transaction)),
                ),
                counts,
            )
            self.assertEqual(after, before)

    def test_database_errors_return_sanitized_503(self):
        class FailingSession:
            def execute(self, statement):
                raise OperationalError(
                    str(statement),
                    {},
                    Exception("private-database-detail"),
                )

            def close(self):
                pass

        def failing_db():
            yield FailingSession()

        app.dependency_overrides[get_db] = failing_db
        response = self.client.post("/risk/assessments", json=self._request())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"detail": "Risk assessment data is temporarily unavailable"},
        )
        self.assertNotIn("private-database-detail", response.text)
        self.assertNotIn("SELECT", response.text)

    def test_invalid_request_timestamps_ranges_and_home_health_endpoints(self):
        naive = self.client.post(
            "/risk/assessments",
            json={
                "start_at": "2026-10-01T00:00:00",
                "end_at": "2026-10-02T00:00:00Z",
            },
        )
        inverted = self.client.post(
            "/risk/assessments",
            json=self._request(self.now + timedelta(days=1), self.now),
        )
        self.assertEqual(naive.status_code, 422)
        self.assertEqual(inverted.status_code, 422)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(
            self.client.get("/health").json(),
            {"status": "healthy", "service": "yottaflex-api"},
        )


if __name__ == "__main__":
    unittest.main()
