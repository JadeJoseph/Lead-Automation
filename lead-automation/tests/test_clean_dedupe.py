import unittest

from leadflow.clean import (canonical_email, clean_email, clean_lead, company_key, extract_phone,
                            name_key, normalise_name, normalise_phone, safe_cell)
from leadflow.dedupe import DedupeIndex


class CleanTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(normalise_name("  JAMES   o'CONNOR "), "James O'Connor")
        self.assertEqual(normalise_name("zoë müller"), "Zoë Müller")
        self.assertEqual(normalise_name("McDonald"), "McDonald")  # mixed case is trusted
        self.assertEqual(normalise_name("anne-marie SMITH"), "Anne-Marie Smith")
        self.assertEqual(normalise_name(None), "")

    def test_email_cleaning(self):
        self.assertEqual(clean_email("  daniel.okafor@northwind.co ")[0], "daniel.okafor@northwind.co")
        self.assertEqual(clean_email("Priya.Sharma@Brightloop.IO")[0], "priya.sharma@brightloop.io")
        email, _, flags = clean_email("sara@@nordicform.se")
        self.assertEqual(email, "sara@nordicform.se")
        self.assertIn("email_repaired_double_at", flags)

    def test_invalid_emails_are_not_guessed(self):
        email, invalid, flags = clean_email("ben.carter@gmail")
        self.assertEqual(email, "")  # we do NOT turn it into gmail.com
        self.assertEqual(invalid, "ben.carter@gmail")
        self.assertIn("email_invalid", flags)
        self.assertEqual(clean_email("")[2], ["email_missing"])
        self.assertEqual(clean_email(None)[2], ["email_missing"])

    def test_canonical_email(self):
        self.assertEqual(canonical_email("tom+automation@acmelogistics.com"), "tom@acmelogistics.com")
        self.assertEqual(canonical_email("J.Smith+x@googlemail.com"), "jsmith@gmail.com")
        self.assertEqual(canonical_email("a.b@corp.com"), "a.b@corp.com")  # dots only ignored on Gmail
        self.assertEqual(canonical_email("nonsense"), "")

    def test_phone(self):
        self.assertEqual(normalise_phone("07700 900123"), "+447700900123")
        self.assertEqual(normalise_phone("+44 (0)7700 900123"), "+447700900123")
        self.assertEqual(normalise_phone("+1 415 555 0132"), "+14155550132")
        self.assertEqual(normalise_phone("12345"), "")
        self.assertEqual(extract_phone("Can someone call me on 07700 900123? Easier than email"), "+447700900123")
        self.assertEqual(extract_phone("we run 12 coffee shops, budget around 8000"), "")

    def test_keys(self):
        self.assertEqual(company_key("Fern & Fox"), company_key("Fern and Fox"))
        self.assertEqual(name_key("Zoë Müller"), name_key("muller zoe"))

    def test_formula_injection_neutralised(self):
        self.assertEqual(safe_cell("=HYPERLINK(\"http://x\")"), "'=HYPERLINK(\"http://x\")")
        self.assertEqual(safe_cell("Hello"), "Hello")

    def test_clean_lead_handles_missing_keys_and_junk(self):
        lead = clean_lead({"id": "X-1", "name": "olivia grant", "company": "Grant & Co",
                           "message": "call me on 07700 900123"})
        self.assertEqual(lead.email, "")
        self.assertEqual(lead.phone, "+447700900123")
        self.assertIn("phone_from_message", lead.flags)
        with self.assertRaises(ValueError):
            clean_lead({"name": "no id"})
        with self.assertRaises(ValueError):
            clean_lead("not a dict")

    def test_bad_timestamp_is_flagged_not_fatal(self):
        lead = clean_lead({"id": "X-2", "received_at": "yesterday-ish", "email": "a@b.co"})
        self.assertIn("received_at_invalid_used_now", lead.flags)


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.idx = DedupeIndex()
        self.idx.add("L-1", "tom@acmelogistics.com", "", "Tom Walsh", "Acme Logistics")
        self.idx.add("L-2", "", "+447700900123", "Olivia Grant", "Grant & Co")

    def test_plus_alias_and_case(self):
        m = self.idx.find("TOM+automation@acmelogistics.com", "", "Tom Walsh", "Acme Logistics")
        self.assertTrue(m and m.hard and m.lead_id == "L-1")

    def test_phone_match(self):
        m = self.idx.find("", "+447700900123", "", "")
        self.assertTrue(m and m.kind == "phone")

    def test_soft_match_is_not_hard(self):
        m = self.idx.find("tom.w@other.com", "", "Walsh Tom", "Acme Logistics Ltd")
        self.assertTrue(m and not m.hard and m.lead_id == "L-1")

    def test_no_match(self):
        self.assertIsNone(self.idx.find("new@person.com", "", "New Person", "NewCo"))

    def test_within_batch(self):
        self.idx.add("L-3", "chloe@fernandfox.com", "", "Chloe Martin", "Fern & Fox")
        self.assertTrue(self.idx.find("CHLOE@fernandfox.com", "", "Chloe Martin", "Fern and Fox").hard)


if __name__ == "__main__":
    unittest.main()
