import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import Base, enable_sqlite_foreign_keys
from models import Account, Customer, Transaction
from synthetic_data import (
    GeneratorConfig,
    ScenarioSpec,
    generate_dataset,
    persist_dataset,
)


class SyntheticDataTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "synthetic-tests.sqlite"
        self.engine = create_engine(f"sqlite:///{database_path}")
        enable_sqlite_foreign_keys(self.engine)
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.config = GeneratorConfig(
            seed=2468,
            run_id="TEST-RUN",
            customer_count=8,
            account_count=12,
            transaction_count=40,
            start_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def tearDown(self):
        self.session.close()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_seed_and_configuration_reproduce_identical_dataset(self):
        first = generate_dataset(self.config)
        second = generate_dataset(self.config)

        self.assertEqual(first.customers, second.customers)
        self.assertEqual(first.accounts, second.accounts)
        self.assertEqual(first.transactions, second.transactions)
        self.assertEqual(first.scenario_labels, second.scenario_labels)
        self.assertEqual(first, second)

    def test_different_seed_changes_ordinary_randomized_activity(self):
        first = generate_dataset(self.config)
        second = generate_dataset(replace(self.config, seed=self.config.seed + 1))

        self.assertNotEqual(first.transactions, second.transactions)

    def test_configuration_rejects_invalid_counts_ids_timestamps_and_thresholds(self):
        invalid_configs = (
            replace(self.config, customer_count=-1),
            replace(self.config, transaction_count=100_001),
            replace(self.config, account_count=1, customer_count=0),
            replace(self.config, transaction_count=1, account_count=0),
            replace(self.config, run_id="bad/run"),
            replace(self.config, run_id=123),
            replace(
                self.config,
                scenarios=(
                    ScenarioSpec("rapid", "rapid_transfers", 2),
                ),
                transaction_count=1,
            ),
            replace(self.config, start_at=datetime(2026, 1, 1)),
            replace(self.config, large_amount_threshold_minor_units=0),
            replace(
                self.config,
                large_amount_threshold_minor_units=2**63 - 1,
            ),
        )
        for config in invalid_configs:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    generate_dataset(config)

    def test_configuration_rejects_invalid_and_duplicate_scenarios(self):
        invalid_scenarios = (
            (
                ScenarioSpec("dup", "rapid_transfers", 2),
                ScenarioSpec("dup", "large_amount", 1, 2_000_000),
            ),
            (ScenarioSpec("invalid/id", "rapid_transfers", 2),),
            (ScenarioSpec("rapid", "rapid_transfers", 2, interval_seconds=301),),
            (ScenarioSpec("rapid", "rapid_transfers", 2, amount_minor_units=0),),
            (ScenarioSpec("large", "large_amount", 2, 2_000_000),),
            (ScenarioSpec("large", "large_amount", 1, 1_000_000),),
        )
        for scenarios in invalid_scenarios:
            config = replace(
                self.config,
                transaction_count=20,
                account_count=4,
                scenarios=scenarios,
            )
            with self.subTest(scenarios=scenarios):
                with self.assertRaises(ValueError):
                    generate_dataset(config)

        insufficient_accounts = replace(
            self.config,
            account_count=2,
            transaction_count=2,
            scenarios=(ScenarioSpec("network", "multi_account_movement", 2),),
        )
        with self.assertRaisesRegex(ValueError, "at least 3 accounts"):
            generate_dataset(insufficient_accounts)

        over_budget = replace(
            self.config,
            transaction_count=2,
            scenarios=(ScenarioSpec("repeat", "repeated_transfers", 3),),
        )
        with self.assertRaisesRegex(ValueError, "exceed configured transaction_count"):
            generate_dataset(over_budget)

    def test_generated_references_and_relationships_are_valid(self):
        dataset = generate_dataset(self.config)
        customer_refs = {customer.customer_ref for customer in dataset.customers}
        account_refs = {account.account_ref for account in dataset.accounts}

        self.assertEqual(len(customer_refs), len(dataset.customers))
        self.assertEqual(len(account_refs), len(dataset.accounts))
        self.assertEqual(
            len({item.transaction_ref for item in dataset.transactions}),
            len(dataset.transactions),
        )
        for account in dataset.accounts:
            self.assertIn(account.customer_ref, customer_refs)
        for transaction in dataset.transactions:
            self.assertIn(transaction.account_ref, account_refs)
            if transaction.counterparty_account_ref is not None:
                self.assertIn(transaction.counterparty_account_ref, account_refs)
                self.assertNotEqual(
                    transaction.account_ref,
                    transaction.counterparty_account_ref,
                )

    def test_currency_amount_and_status_values_are_valid_and_balances_unchanged(self):
        dataset = generate_dataset(self.config)
        accounts = {account.account_ref: account for account in dataset.accounts}
        self.assertTrue(
            all(
                account.currency in {"INR", "USD", "EUR"}
                and account.balance_minor_units == 0
                for account in dataset.accounts
            )
        )
        for transaction in dataset.transactions:
            self.assertIs(type(transaction.amount_minor_units), int)
            self.assertGreater(transaction.amount_minor_units, 0)
            self.assertLessEqual(transaction.amount_minor_units, 2**63 - 1)
            self.assertIn(transaction.currency, {"INR", "USD", "EUR"})
            self.assertEqual(
                transaction.currency,
                accounts[transaction.account_ref].currency,
            )
            if transaction.counterparty_account_ref is not None:
                self.assertEqual(
                    transaction.currency,
                    accounts[transaction.counterparty_account_ref].currency,
                )
            self.assertIsNotNone(transaction.occurred_at.tzinfo)
            self.assertIsNotNone(transaction.created_at.tzinfo)
            self.assertGreaterEqual(
                transaction.created_at,
                transaction.occurred_at,
            )
            self.assertIn(
                transaction.transaction_type,
                {"transfer", "card_payment", "cash_withdrawal", "deposit"},
            )
            self.assertIn(
                transaction.status,
                {"completed", "pending", "failed", "reversed"},
            )

    def test_scenario_builders_create_patterns_and_return_nonfraud_labels(self):
        config = replace(
            self.config,
            account_count=4,
            transaction_count=12,
            scenarios=(
                ScenarioSpec("fast", "rapid_transfers", 3, interval_seconds=10),
                ScenarioSpec(
                    "repeat",
                    "repeated_transfers",
                    3,
                    interval_seconds=3_600,
                ),
                ScenarioSpec(
                    "large",
                    "large_amount",
                    1,
                    amount_minor_units=2_000_000,
                ),
                ScenarioSpec(
                    "network",
                    "multi_account_movement",
                    2,
                ),
            ),
        )
        dataset = generate_dataset(config)
        transactions_by_ref = {
            transaction.transaction_ref: transaction
            for transaction in dataset.transactions
        }
        account_by_ref = {account.account_ref: account for account in dataset.accounts}

        self.assertEqual(len(dataset.scenario_labels), 9)
        self.assertTrue(set(dataset.scenario_labels).issubset(transactions_by_ref))
        self.assertEqual(
            sum(label == ("fast",) for label in dataset.scenario_labels.values()),
            3,
        )
        fast_transactions = [
            transactions_by_ref[ref]
            for ref, labels in dataset.scenario_labels.items()
            if labels == ("fast",)
        ]
        self.assertEqual(len(fast_transactions), 3)
        self.assertLessEqual(
            (fast_transactions[1].occurred_at - fast_transactions[0].occurred_at)
            .total_seconds(),
            300,
        )
        self.assertEqual(
            fast_transactions[1].occurred_at - fast_transactions[0].occurred_at,
            fast_transactions[2].occurred_at - fast_transactions[1].occurred_at,
        )
        repeated_transactions = [
            transactions_by_ref[ref]
            for ref, labels in dataset.scenario_labels.items()
            if labels == ("repeat",)
        ]
        self.assertEqual(len(repeated_transactions), 3)
        self.assertEqual(
            {
                (item.account_ref, item.counterparty_account_ref)
                for item in repeated_transactions
            },
            {
                (
                    repeated_transactions[0].account_ref,
                    repeated_transactions[0].counterparty_account_ref,
                )
            },
        )
        self.assertTrue(
            all(item.transaction_type == "transfer" for item in repeated_transactions)
        )
        large_ref = next(
            ref
            for ref, labels in dataset.scenario_labels.items()
            if labels == ("large",)
        )
        self.assertGreater(
            transactions_by_ref[large_ref].amount_minor_units,
            config.large_amount_threshold_minor_units,
        )
        network_transactions = [
            transactions_by_ref[ref]
            for ref, labels in dataset.scenario_labels.items()
            if labels == ("network",)
        ]
        network_accounts = {
            account_ref
            for item in network_transactions
            for account_ref in (item.account_ref, item.counterparty_account_ref)
        }
        self.assertGreaterEqual(len(network_accounts), 3)
        self.assertTrue(
            all(item.transaction_type == "transfer" for item in network_transactions)
        )
        self.assertTrue(
            all(account_by_ref[ref].status == "active" for ref in network_accounts)
        )
        expected_scenario_counts = {
            "fast": 3,
            "repeat": 3,
            "large": 1,
            "network": 2,
        }
        for scenario_id, expected_count in expected_scenario_counts.items():
            labeled_refs = {
                ref
                for ref, labels in dataset.scenario_labels.items()
                if labels == (scenario_id,)
            }
            self.assertEqual(len(labeled_refs), expected_count)
            self.assertTrue(labeled_refs.issubset(transactions_by_ref))

    def test_all_scenario_accounts_are_active_and_not_randomly_frozen_or_closed(self):
        config = replace(
            self.config,
            account_count=4,
            transaction_count=5,
            scenarios=(
                ScenarioSpec("rapid", "rapid_transfers", 2),
                ScenarioSpec("big", "large_amount", 1, 2_000_000),
                ScenarioSpec("chain", "multi_account_movement", 2),
            ),
        )
        dataset = generate_dataset(config)
        accounts = {account.account_ref: account for account in dataset.accounts}
        scenario_account_refs = {
            account_ref
            for transaction_ref in dataset.scenario_labels
            for transaction in dataset.transactions
            if transaction.transaction_ref == transaction_ref
            for account_ref in (
                transaction.account_ref,
                transaction.counterparty_account_ref,
            )
            if account_ref is not None
        }

        self.assertEqual(len(scenario_account_refs), 3)
        self.assertTrue(
            all(
                accounts[account_ref].status == "active"
                for account_ref in scenario_account_refs
            )
        )

    def test_persistence_resolves_foreign_keys_and_does_not_change_balances(self):
        dataset = generate_dataset(self.config)
        initial_balances = {
            account.account_ref: account.balance_minor_units
            for account in dataset.accounts
        }
        existing_customer = Customer(
            customer_ref="SYN-PREEXISTING-CUSTOMER",
            full_name="Synthetic Existing Customer",
            country="US",
        )
        self.session.add(existing_customer)
        self.session.flush()
        existing_account = Account(
            account_ref="SYN-PREEXISTING-ACCOUNT",
            customer_id=existing_customer.id,
            account_type="savings",
            currency="INR",
            balance_minor_units=987654,
        )
        self.session.add(existing_account)
        self.session.commit()

        persisted = persist_dataset(dataset, self.session)

        self.assertEqual(len(persisted.customer_ids), len(dataset.customers))
        self.assertEqual(len(persisted.account_ids), len(dataset.accounts))
        self.assertEqual(len(persisted.transaction_ids), len(dataset.transactions))
        self.assertEqual(
            self.session.query(Customer).count(),
            len(dataset.customers) + 1,
        )
        self.assertEqual(
            self.session.query(Account).count(),
            len(dataset.accounts) + 1,
        )
        self.assertEqual(
            self.session.query(Transaction).count(),
            len(dataset.transactions),
        )
        for account in (
            self.session.query(Account)
            .filter(Account.account_ref.in_(initial_balances))
            .all()
        ):
            self.assertEqual(
                account.balance_minor_units,
                initial_balances[account.account_ref],
            )
        self.session.refresh(existing_account)
        self.assertEqual(existing_account.balance_minor_units, 987654)
        customer_ids = {
            customer.customer_ref: customer.id
            for customer in self.session.query(Customer).all()
        }
        account_ids = {
            account.account_ref: account.id
            for account in self.session.query(Account).all()
        }
        for item in dataset.accounts:
            stored = self.session.get(Account, account_ids[item.account_ref])
            self.assertEqual(stored.customer_id, customer_ids[item.customer_ref])
        for item in dataset.transactions:
            stored = self.session.get(
                Transaction,
                persisted.transaction_ids[item.transaction_ref],
            )
            self.assertEqual(stored.account_id, account_ids[item.account_ref])
            if item.counterparty_account_ref is not None:
                self.assertEqual(
                    stored.counterparty_account_id,
                    account_ids[item.counterparty_account_ref],
                )

    def test_same_run_id_is_rejected_without_duplicate_records(self):
        dataset = generate_dataset(self.config)
        persist_dataset(dataset, self.session)

        with self.assertRaises(ValueError):
            persist_dataset(dataset, self.session)

        self.assertEqual(self.session.query(Customer).count(), len(dataset.customers))
        self.assertEqual(self.session.query(Account).count(), len(dataset.accounts))
        self.assertEqual(
            self.session.query(Transaction).count(),
            len(dataset.transactions),
        )

    def test_duplicate_references_in_dataset_fail_without_partial_insertion(self):
        dataset = generate_dataset(self.config)
        dataset_with_duplicate_customer = replace(
            dataset,
            customers=dataset.customers + (dataset.customers[0],),
        )

        with self.assertRaisesRegex(ValueError, "customer references are not unique"):
            persist_dataset(dataset_with_duplicate_customer, self.session)

        self.assertEqual(self.session.query(Customer).count(), 0)
        self.assertEqual(self.session.query(Account).count(), 0)
        self.assertEqual(self.session.query(Transaction).count(), 0)

    def test_database_failure_rolls_back_the_entire_dataset(self):
        dataset = generate_dataset(self.config)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TRIGGER reject_test_account BEFORE INSERT ON accounts "
                    "BEGIN SELECT RAISE(ABORT, 'forced test failure'); END"
                )
            )

        with self.assertRaises(IntegrityError):
            persist_dataset(dataset, self.session)

        self.assertEqual(self.session.query(Customer).count(), 0)
        self.assertEqual(self.session.query(Account).count(), 0)
        self.assertEqual(self.session.query(Transaction).count(), 0)


if __name__ == "__main__":
    unittest.main()
