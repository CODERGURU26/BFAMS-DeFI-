import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.integrations.setu.exceptions import SetuMalformedResponseError
from app.integrations.setu.normalizer import normalize_setu_fi_response
from app.integrations.setu.parser import parse_setu_fi_response


def fi_payload(account_records):
    return {
        "status": "COMPLETED",
        "format": "json",
        "id": "synthetic-session",
        "consentId": "synthetic-consent",
        "fips": [{"fipID": "synthetic-fip", "accounts": account_records}],
    }


def account_record(
    account_id="synthetic-account",
    transactions=None,
    account_type="deposit",
):
    return {
        "status": "DELIVERED",
        "data": {
            "account": {
                "linkedAccRef": account_id,
                "type": account_type,
                "profile": {
                    "holders": {
                        "type": "SINGLE",
                        "holder": {"name": "Synthetic Holder"},
                    }
                },
                "summary": {"status": "ACTIVE", "currency": "INR"},
                "transactions": {"transaction": transactions or []},
            }
        },
    }


def transaction_record(
    transaction_id="synthetic-txn",
    transaction_type="CREDIT",
    amount="123.4500",
    timestamp="2026-01-10T12:30:00+05:30",
):
    return {
        "txnId": transaction_id,
        "type": transaction_type,
        "amount": amount,
        "narration": "synthetic description",
        "transactionTimestamp": timestamp,
    }


class TestSetuNormalizer(unittest.TestCase):
    def test_normalizes_deposit_account_and_credit_debit_transactions(self):
        payload = fi_payload(
            [
                account_record(
                    transactions=[
                        transaction_record(),
                        transaction_record(
                            transaction_id="synthetic-debit",
                            transaction_type="DEBIT",
                            amount="10.25",
                        ),
                    ]
                )
            ]
        )

        accounts, transactions_by_account, rejected = normalize_setu_fi_response(
            payload
        )

        self.assertEqual(rejected, [])
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].external_account_id, "synthetic-account")
        self.assertEqual(accounts[0].account_type, "deposit")
        self.assertEqual(accounts[0].status, "active")
        self.assertEqual(accounts[0].currency, "INR")
        self.assertIsNone(accounts[0].account_name)
        self.assertIsNone(accounts[0].source_updated_at)
        transactions = transactions_by_account["synthetic-account"]
        self.assertEqual(
            [transaction.transaction_type for transaction in transactions],
            ["credit", "debit"],
        )
        self.assertEqual(transactions[0].amount, Decimal("123.4500"))
        self.assertEqual(transactions[0].currency, "INR")
        self.assertEqual(transactions[0].description, "synthetic description")
        self.assertIsNone(transactions[0].source_updated_at)
        self.assertEqual(
            transactions[0].transaction_at,
            datetime(
                2026,
                1,
                10,
                12,
                30,
                tzinfo=timezone(timedelta(hours=5, minutes=30)),
            ),
        )

    def test_normalizes_official_session_response_account_shape(self):
        payload = fi_payload(
            [
                {
                    "linkRefNumber": "stable-link-ref",
                    "data": {
                        "account": {
                            "linkedAccRef": "stable-link-ref",
                            "type": "deposit",
                            "summary": {
                                "status": "ACTIVE",
                                "currency": "INR",
                            },
                            "transactions": {
                                "transaction": [transaction_record()]
                            },
                        }
                    },
                }
            ]
        )

        accounts, transactions_by_account, rejected = normalize_setu_fi_response(
            payload
        )

        self.assertEqual(rejected, [])
        self.assertEqual(accounts[0].external_account_id, "stable-link-ref")
        self.assertEqual(len(transactions_by_account["stable-link-ref"]), 1)

    def test_does_not_accept_webhook_only_decrypted_fi_shape(self):
        payload = fi_payload(
            [
                {
                    "linkRefNumber": "stable-link-ref",
                    "data": {
                        "decryptedFI": {
                            "account": {
                                "linkedAccRef": "stable-link-ref",
                                "type": "deposit",
                            }
                        }
                    },
                }
            ]
        )

        with self.assertRaises(SetuMalformedResponseError):
            parse_setu_fi_response(payload)

    def test_rejects_unstable_ids_bad_amounts_directions_and_naive_times(self):
        payload = fi_payload(
            [
                {
                    "status": "DELIVERED",
                    "data": {
                        "account": {
                            "type": "deposit",
                            "maskedAccNumber": "must-not-be-used",
                            "transactions": {"transaction": []},
                        }
                    },
                },
                account_record(
                    account_id="account-two",
                    transactions=[
                        transaction_record(transaction_id=None),
                        transaction_record(
                            transaction_id="bad-amount",
                            amount="-4",
                        ),
                        transaction_record(
                            transaction_id="bad-direction",
                            transaction_type="TRANSFER",
                        ),
                        transaction_record(
                            transaction_id="naive-time",
                            timestamp="2026-01-10T12:30:00",
                        ),
                    ],
                ),
            ]
        )

        accounts, transactions_by_account, rejected = normalize_setu_fi_response(
            payload
        )

        self.assertEqual(len(accounts), 1)
        self.assertEqual(transactions_by_account.get("account-two", []), [])
        self.assertEqual(
            [item["reason"] for item in rejected],
            [
                "missing_stable_account_id",
                "invalid_transaction",
                "invalid_transaction",
                "invalid_transaction",
                "invalid_transaction",
            ],
        )
        self.assertNotIn("must-not-be-used", str(rejected))

    def test_rejects_unsupported_holdings_account_type(self):
        accounts, transactions_by_account, rejected = normalize_setu_fi_response(
            fi_payload(
                [
                    account_record(
                        account_id="investment-account",
                        account_type="equities",
                    )
                ]
            )
        )

        self.assertEqual(accounts, [])
        self.assertEqual(transactions_by_account, {})
        self.assertEqual(
            rejected,
            [{"index": 0, "reason": "unsupported_account_type"}],
        )

    def test_empty_fi_response_returns_empty_normalized_results(self):
        accounts, transactions_by_account, rejected = normalize_setu_fi_response(
            fi_payload([])
        )

        self.assertEqual(accounts, [])
        self.assertEqual(transactions_by_account, {})
        self.assertEqual(rejected, [])

    def test_multiple_accounts_and_partial_response_skip_failed_or_null_data(self):
        payload = {
            "status": "PARTIAL",
            "format": "json",
            "id": "synthetic-session",
            "consentId": "synthetic-consent",
            "fips": [
                {
                    "fipID": "synthetic-fip",
                    "accounts": [
                        account_record(
                            account_id="account-one",
                            transactions=[
                                transaction_record(transaction_id="txn-one")
                            ],
                        ),
                        account_record(
                            account_id="account-two",
                            transactions=[
                                transaction_record(transaction_id="txn-two")
                            ],
                        ),
                        {
                            "linkRefNumber": "failed-account-ref",
                            "status": "FAILED",
                            "data": None,
                        },
                        {
                            "linkRefNumber": "pending-account-ref",
                            "status": "PENDING",
                            "data": None,
                        },
                        {
                            "linkRefNumber": "failed-fi-status-ref",
                            "FIstatus": "FAILED",
                            "data": {
                                "account": {
                                    "linkedAccRef": "failed-fi-status-ref",
                                    "type": "deposit",
                                }
                            },
                        },
                    ],
                }
            ],
        }

        accounts, transactions_by_account, rejected = normalize_setu_fi_response(
            payload
        )

        self.assertEqual(
            [account.external_account_id for account in accounts],
            ["account-one", "account-two"],
        )
        self.assertEqual(set(transactions_by_account), {"account-one", "account-two"})
        self.assertEqual(
            [len(items) for items in transactions_by_account.values()],
            [1, 1],
        )
        self.assertEqual(rejected, [])

    def test_rejects_malformed_top_level_or_nested_shapes(self):
        for payload in (
            {},
            {"fips": [{}]},
            {"fips": [{"accounts": [{"data": {"account": []}}]}]},
        ):
            with self.subTest(payload_keys=tuple(payload)):
                with self.assertRaises(SetuMalformedResponseError):
                    parse_setu_fi_response(payload)


if __name__ == "__main__":
    unittest.main()
