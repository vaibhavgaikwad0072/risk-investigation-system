import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import Base, enable_sqlite_foreign_keys
from models import Account, Customer, Transaction


class TransactionModelTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "transaction-tests.sqlite"
        self.engine = create_engine(f"sqlite:///{database_path}")
        enable_sqlite_foreign_keys(self.engine)
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)

        self.customer = Customer(
            customer_ref="SYN-TX-CUSTOMER",
            full_name="Synthetic Transaction Customer",
            country="IN",
        )
        self.session.add(self.customer)
        self.session.flush()
        self.account = Account(
            account_ref="SYN-TX-ACCOUNT-1",
            customer_id=self.customer.id,
            account_type="savings",
            currency="INR",
        )
        self.other_account = Account(
            account_ref="SYN-TX-ACCOUNT-2",
            customer_id=self.customer.id,
            account_type="current",
            currency="INR",
        )
        self.session.add_all([self.account, self.other_account])
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _transaction(self, **overrides):
        fields = {
            "transaction_ref": "SYN-TX-001",
            "account_id": self.account.id,
            "amount_minor_units": 1250,
            "currency": "INR",
            "transaction_type": "card_payment",
            "occurred_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
        fields.update(overrides)
        return Transaction(**fields)

    def _assert_raw_insert_fails(self, transaction_type, status, currency, amount):
        connection = self.engine.connect()
        transaction = connection.begin()
        try:
            with self.assertRaises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO transactions "
                        "(transaction_ref, account_id, amount_minor_units, "
                        "currency, transaction_type, status, occurred_at, "
                        "created_at) VALUES "
                        "(:transaction_ref, :account_id, :amount, :currency, "
                        ":transaction_type, :status, :occurred_at, :created_at)"
                    ),
                    {
                        "transaction_ref": "SYN-TX-CHECK",
                        "account_id": self.account.id,
                        "amount": amount,
                        "currency": currency,
                        "transaction_type": transaction_type,
                        "status": status,
                        "occurred_at": "2026-01-01 00:00:00",
                        "created_at": "2026-01-01 00:00:00",
                    },
                )
            transaction.rollback()
        finally:
            connection.close()

    def test_transactions_table_and_account_foreign_keys_are_created(self):
        inspector = inspect(self.engine)
        self.assertIn("transactions", inspector.get_table_names())
        foreign_keys = inspector.get_foreign_keys("transactions")
        self.assertEqual(len(foreign_keys), 2)
        for foreign_key in foreign_keys:
            self.assertEqual(foreign_key["referred_table"], "accounts")
            self.assertEqual(foreign_key["referred_columns"], ["id"])
            self.assertEqual(foreign_key["options"].get("ondelete"), "RESTRICT")

    def test_valid_transaction_can_be_retrieved_with_both_accounts(self):
        transaction = self._transaction(
            transaction_type="transfer",
            counterparty_account_id=self.other_account.id,
            merchant_ref=None,
        )
        self.session.add(transaction)
        self.session.commit()

        retrieved = self.session.get(Transaction, transaction.id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.transaction_ref, "SYN-TX-001")
        self.assertEqual(retrieved.account_id, self.account.id)
        self.assertEqual(retrieved.counterparty_account_id, self.other_account.id)
        self.assertEqual(retrieved.amount_minor_units, 1250)

    def test_missing_account_foreign_keys_are_rejected(self):
        for values in (
            {"account_id": 999999},
            {"counterparty_account_id": 999999},
        ):
            with self.subTest(values=values):
                self.session.add(self._transaction(**values))
                with self.assertRaises(IntegrityError):
                    self.session.commit()
                self.session.rollback()

    def test_duplicate_transaction_reference_is_rejected(self):
        self.session.add(self._transaction())
        self.session.commit()

        self.session.add(
            self._transaction(
                account_id=self.other_account.id,
                transaction_type="cash_withdrawal",
            )
        )
        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()

    def test_transaction_can_omit_counterparty_and_merchant(self):
        transaction = self._transaction()
        self.session.add(transaction)
        self.session.commit()

        retrieved = self.session.get(Transaction, transaction.id)
        self.assertIsNone(retrieved.counterparty_account_id)
        self.assertIsNone(retrieved.merchant_ref)

    def test_invalid_type_and_status_are_rejected_by_database(self):
        self._assert_raw_insert_fails("unknown", "pending", "INR", 100)
        self._assert_raw_insert_fails("deposit", "unknown", "INR", 100)

    def test_invalid_currency_format_is_rejected_by_database(self):
        self._assert_raw_insert_fails("deposit", "pending", "inr", 100)
        self._assert_raw_insert_fails("deposit", "pending", "IN", 100)

    def test_nonpositive_amounts_are_rejected_by_database(self):
        self._assert_raw_insert_fails("deposit", "pending", "INR", 0)
        self._assert_raw_insert_fails("deposit", "pending", "INR", -1)

    def test_unsupported_currency_is_rejected_by_model_validation(self):
        with self.assertRaises(ValueError):
            self._transaction(currency="GBP")

    def test_transfer_requires_a_distinct_counterparty_account(self):
        for counterparty_id in (None, self.account.id):
            with self.subTest(counterparty_id=counterparty_id):
                self.session.add(
                    self._transaction(
                        counterparty_account_id=counterparty_id,
                        transaction_type="transfer",
                    )
                )
                with self.assertRaises(IntegrityError):
                    self.session.commit()
                self.session.rollback()

    def test_status_default_and_utc_creation_timestamp(self):
        transaction = self._transaction()
        self.session.add(transaction)
        self.session.commit()

        self.assertEqual(transaction.status, "pending")
        self.assertIsNotNone(transaction.created_at)
        default_created_at = Transaction.__table__.c.created_at.default.arg(None)
        self.assertIsNotNone(default_created_at.tzinfo)
        self.assertEqual(
            default_created_at.utcoffset(),
            timezone.utc.utcoffset(None),
        )


if __name__ == "__main__":
    unittest.main()
