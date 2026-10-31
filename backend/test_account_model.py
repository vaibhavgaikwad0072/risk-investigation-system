import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import Base, enable_sqlite_foreign_keys
from models import Account, Customer


class AccountModelTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "account-tests.sqlite"
        self.engine = create_engine(f"sqlite:///{database_path}")
        enable_sqlite_foreign_keys(self.engine)
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.customer = Customer(
            customer_ref="SYN-ACCOUNT-CUSTOMER",
            full_name="Synthetic Account Customer",
            country="IN",
        )
        self.session.add(self.customer)
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _account(self, **overrides):
        fields = {
            "account_ref": "SYN-ACCOUNT-001",
            "customer_id": self.customer.id,
            "account_type": "savings",
            "currency": "INR",
        }
        fields.update(overrides)
        return Account(**fields)

    def _assert_raw_insert_fails(self, account_type, currency, status):
        connection = self.engine.connect()
        transaction = connection.begin()
        try:
            with self.assertRaises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO accounts "
                        "(account_ref, customer_id, account_type, currency, "
                        "balance_minor_units, status, created_at) "
                        "VALUES (:account_ref, :customer_id, :account_type, "
                        ":currency, 0, :status, :created_at)"
                    ),
                    {
                        "account_ref": "SYN-CHECK-CONSTRAINT",
                        "customer_id": self.customer.id,
                        "account_type": account_type,
                        "currency": currency,
                        "status": status,
                        "created_at": "2026-01-01 00:00:00",
                    },
                )
            transaction.rollback()
        finally:
            connection.close()

    def test_account_table_and_customer_foreign_key_are_created(self):
        inspector = inspect(self.engine)

        self.assertIn("accounts", inspector.get_table_names())
        foreign_keys = inspector.get_foreign_keys("accounts")
        self.assertEqual(len(foreign_keys), 1)
        self.assertEqual(foreign_keys[0]["constrained_columns"], ["customer_id"])
        self.assertEqual(foreign_keys[0]["referred_table"], "customers")
        self.assertEqual(foreign_keys[0]["referred_columns"], ["id"])
        self.assertEqual(foreign_keys[0]["options"].get("ondelete"), "RESTRICT")

    def test_valid_account_can_be_retrieved_with_customer_reference(self):
        account = self._account()
        self.session.add(account)
        self.session.commit()

        self.assertIsNotNone(account.id)
        retrieved = self.session.get(Account, account.id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.account_ref, "SYN-ACCOUNT-001")
        self.assertEqual(retrieved.customer_id, self.customer.id)
        self.assertEqual(retrieved.currency, "INR")

    def test_nonexistent_customer_is_rejected(self):
        self.session.add(self._account(customer_id=999999))
        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()
        self.assertEqual(self.session.query(Account).count(), 0)

    def test_sqlite_foreign_keys_are_enabled_for_each_connection(self):
        for _ in range(2):
            with self.engine.connect() as connection:
                self.assertEqual(
                    connection.exec_driver_sql("PRAGMA foreign_keys").scalar(),
                    1,
                )
            self.engine.dispose()

    def test_duplicate_account_reference_is_rejected(self):
        self.session.add(self._account())
        self.session.commit()

        self.session.add(
            self._account(
                account_ref="SYN-ACCOUNT-001",
                account_type="current",
            )
        )
        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()
        self.assertEqual(self.session.query(Account).count(), 1)

    def test_invalid_account_type_is_rejected_by_database(self):
        self._assert_raw_insert_fails(
            account_type="investment",
            currency="INR",
            status="active",
        )

    def test_invalid_status_is_rejected_by_database(self):
        self._assert_raw_insert_fails(
            account_type="savings",
            currency="INR",
            status="pending",
        )

    def test_invalid_currency_format_is_rejected_by_database(self):
        self._assert_raw_insert_fails(
            account_type="savings",
            currency="inr",
            status="active",
        )

    def test_unsupported_but_well_formed_currency_is_rejected_by_model_validation(self):
        with self.assertRaises(ValueError):
            self._account(currency="GBP")

    def test_defaults_and_negative_integer_balances(self):
        default_account = self._account()
        negative_account = self._account(
            account_ref="SYN-ACCOUNT-NEGATIVE",
            balance_minor_units=-1250,
            status="frozen",
        )
        self.session.add_all([default_account, negative_account])
        self.session.commit()

        self.assertEqual(default_account.balance_minor_units, 0)
        self.assertEqual(default_account.status, "active")
        self.assertIsNotNone(default_account.created_at)
        self.assertEqual(negative_account.balance_minor_units, -1250)

    def test_customer_can_have_multiple_accounts(self):
        self.session.add_all(
            [
                self._account(account_ref="SYN-ACCOUNT-MULTI-1"),
                self._account(
                    account_ref="SYN-ACCOUNT-MULTI-2",
                    account_type="current",
                ),
            ]
        )
        self.session.commit()

        accounts = (
            self.session.query(Account)
            .filter(Account.customer_id == self.customer.id)
            .all()
        )
        self.assertEqual(len(accounts), 2)

    def test_customer_delete_is_restricted_while_accounts_exist(self):
        self.session.add(self._account())
        self.session.commit()

        self.session.delete(self.customer)
        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()
        self.assertIsNotNone(self.session.get(Customer, self.customer.id))


if __name__ == "__main__":
    unittest.main()
