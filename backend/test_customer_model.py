import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import Base
from models import Customer


class CustomerModelTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "customer-tests.sqlite"
        self.engine = create_engine(f"sqlite:///{database_path}")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _assert_insert_rejected_by_not_null(self, missing_column):
        values = {
            "customer_ref": "SYN-REQUIRED-CHECK",
            "full_name": "Synthetic Required Check",
            "email": None,
            "country": "US",
            "created_at": "2026-01-01 00:00:00",
            "is_active": 1,
        }
        values[missing_column] = None
        columns = ", ".join(values)
        placeholders = ", ".join(f":{column}" for column in values)

        connection = self.engine.connect()
        transaction = connection.begin()
        try:
            with self.assertRaises(IntegrityError):
                connection.execute(
                    text(
                        f"INSERT INTO customers ({columns}) "
                        f"VALUES ({placeholders})"
                    ),
                    values,
                )
            transaction.rollback()
        finally:
            connection.close()

    def test_customers_table_and_expected_columns_are_created(self):
        inspector = inspect(self.engine)

        self.assertIn("customers", inspector.get_table_names())
        self.assertEqual(
            {column["name"] for column in inspector.get_columns("customers")},
            {
                "id",
                "customer_ref",
                "full_name",
                "email",
                "country",
                "created_at",
                "is_active",
            },
        )

    def test_valid_synthetic_customer_can_be_retrieved_with_generated_id(self):
        customer = Customer(
            customer_ref="SYN-CUSTOMER-001",
            full_name="Synthetic Customer One",
            email="one@example.test",
            country="US",
        )
        self.session.add(customer)
        self.session.commit()

        self.assertIsNotNone(customer.id)
        retrieved = self.session.get(Customer, customer.id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.customer_ref, "SYN-CUSTOMER-001")
        self.assertEqual(retrieved.full_name, "Synthetic Customer One")
        self.assertEqual(retrieved.email, "one@example.test")
        self.assertEqual(retrieved.country, "US")

    def test_customer_reference_rejects_duplicate_values_and_rolls_back(self):
        self.session.add(
            Customer(
                customer_ref="SYN-CUSTOMER-DUP",
                full_name="Synthetic Customer Duplicate One",
                country="US",
            )
        )
        self.session.commit()

        self.session.add(
            Customer(
                customer_ref="SYN-CUSTOMER-DUP",
                full_name="Synthetic Customer Duplicate Two",
                country="CA",
            )
        )
        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()
        self.assertEqual(self.session.query(Customer).count(), 1)

    def test_non_null_email_rejects_duplicate_values_and_rolls_back(self):
        self.session.add(
            Customer(
                customer_ref="SYN-CUSTOMER-EMAIL-1",
                full_name="Synthetic Email One",
                email="shared@example.test",
                country="US",
            )
        )
        self.session.commit()

        self.session.add(
            Customer(
                customer_ref="SYN-CUSTOMER-EMAIL-2",
                full_name="Synthetic Email Two",
                email="shared@example.test",
                country="CA",
            )
        )
        with self.assertRaises(IntegrityError):
            self.session.commit()
        self.session.rollback()
        self.assertEqual(self.session.query(Customer).count(), 1)

    def test_customer_without_email_can_be_stored(self):
        customer = Customer(
            customer_ref="SYN-CUSTOMER-NO-EMAIL",
            full_name="Synthetic Customer No Email",
            country="GB",
        )
        self.session.add(customer)
        self.session.commit()

        retrieved = self.session.get(Customer, customer.id)
        self.assertIsNotNone(retrieved)
        self.assertIsNone(retrieved.email)

    def test_required_fields_reject_null_values_at_database_layer(self):
        for column in ("customer_ref", "full_name", "country", "created_at", "is_active"):
            with self.subTest(column=column):
                self._assert_insert_rejected_by_not_null(column)

    def test_active_status_defaults_to_true(self):
        customer = Customer(
            customer_ref="SYN-CUSTOMER-ACTIVE",
            full_name="Synthetic Active Customer",
            country="US",
        )
        self.session.add(customer)
        self.session.commit()

        self.assertTrue(customer.is_active)

    def test_creation_timestamp_is_populated(self):
        customer = Customer(
            customer_ref="SYN-CUSTOMER-TIMESTAMP",
            full_name="Synthetic Timestamp Customer",
            country="US",
        )
        self.session.add(customer)
        self.session.commit()

        self.assertIsNotNone(customer.created_at)


if __name__ == "__main__":
    unittest.main()
