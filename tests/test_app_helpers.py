import unittest
from decimal import Decimal

from app.app import normalize_form, parse_files_json


class NormalizeFormTest(unittest.TestCase):
    def test_total_without_payer_split_remains_unassigned(self):
        data = normalize_form(
            {
                "description": "Material",
                "category": "Materiali",
                "total": "42,50",
                "ana_paid": "",
                "jakob_paid": "",
            }
        )

        self.assertEqual(data["total"], Decimal("42.50"))
        self.assertEqual(data["ana_paid"], Decimal("0"))
        self.assertEqual(data["jakob_paid"], Decimal("0"))

    def test_split_amounts_recalculate_total(self):
        data = normalize_form(
            {
                "description": "Material",
                "category": "Materiali",
                "total": "100.00",
                "ana_paid": "25.25",
                "jakob_paid": "30,75",
            }
        )

        self.assertEqual(data["total"], Decimal("56.00"))
        self.assertEqual(data["ana_paid"], Decimal("25.25"))
        self.assertEqual(data["jakob_paid"], Decimal("30.75"))


class ParseFilesJsonTest(unittest.TestCase):
    def test_rejects_non_list_payloads(self):
        self.assertEqual(parse_files_json('{"file_name": "receipt.jpg"}'), [])

    def test_keeps_upload_token_for_pending_files(self):
        files = parse_files_json(
            '[{"file_name": "stored.jpg", "original_name": "receipt.jpg", "upload_token": "abc123"}]'
        )

        self.assertEqual(
            files,
            [{"file_name": "stored.jpg", "original_name": "receipt.jpg", "upload_token": "abc123"}],
        )


if __name__ == "__main__":
    unittest.main()
