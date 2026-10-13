"""The workflow: one lead in, one auditable outcome out.

clean -> validate -> dedupe lookup -> triage -> (spam | duplicate | add) -> notify

Each stage is wrapped so one bad lead can never stop the batch; the failing stage and reason
land in the review queue.
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field

from .clean import Lead, clean_lead, safe_cell
from .dedupe import DedupeIndex
from .llm import Triager
from .notify import Notifier
from .store import Store, now_iso

RANK = {"Cold": 0, "Warm": 1, "Hot": 2}


@dataclass
class Outcome:
    source_id: str
    outcome: str  # added | duplicate | rejected | spam | error | skipped
    lead_id: str = ""
    tag: str = ""
    detail: str = ""
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__


class Pipeline:
    def __init__(self, cfg, store: Store, triager: Triager, notifier: Notifier, run_id: str):
        self.cfg, self.store, self.triager, self.notifier, self.run_id = cfg, store, triager, notifier, run_id
        self.index = DedupeIndex()
        for r in store.rows:
            self.index.add(r["lead_id"], r["email"], r.get("phone", ""), r["name"], r["company"])

    # ------------------------------------------------------------------ helpers
    def _log(self, source_id, stage, outcome, reason="", lead: Lead | None = None, lead_id="", matched="", raw=None):
        self.store.log(self.run_id, source_id, stage, outcome, reason, lead_id=lead_id, matched=matched,
                       name=lead.name if lead else "", email=(lead.email or lead.invalid_email) if lead else "",
                       message=lead.message if lead else "", raw=raw)

    def _notify(self, row: dict, source_id: str, note: str = "") -> list:
        failures = []
        for channel, ok, err in self.notifier.notify_hot(row, note):
            if not ok:
                failures.append(channel)
                self._log(source_id, "notify", "notify_failed", f"{channel}: {err}", lead_id=row["lead_id"], raw=None)
        return failures

    # ------------------------------------------------------------------ main entry
    def process(self, raw) -> Outcome:
        sid = str(raw.get("id", "")).strip() if isinstance(raw, dict) else ""
        stage = "ingest"
        lead: Lead | None = None
        try:
            if not sid:
                reason = "payload is not an object or has no 'id'"
                self._log("(unknown)", stage, "rejected", reason, raw=raw)
                return Outcome("(unknown)", "rejected", detail=reason)

            prior = self.store.already_processed(sid)
            if prior:
                return Outcome(sid, "skipped", detail=f"already processed earlier ({prior})")

            stage = "clean"
            lead = clean_lead(raw, self.cfg.default_region)

            stage = "validate"
            if not (lead.email or lead.phone):
                bits = []
                if lead.invalid_email:
                    bits.append(f"email '{lead.invalid_email}' is not a valid address")
                else:
                    bits.append("no email")
                bits.append("no phone number found")
                reason = "No usable contact method: " + ", ".join(bits) + ". Needs a human to follow up."
                self._log(sid, stage, "rejected", reason, lead, raw=raw)
                return Outcome(sid, "rejected", detail=reason)

            stage = "dedupe"
            match = self.index.find(lead.email, lead.phone, lead.name, lead.company)

            stage = "triage"
            triage = self.triager.triage(lead)
            if "llm_fallback" in triage.flags:
                self._log(sid, stage, "llm_fallback", f"model failed, used rules: {triage.llm_error}", lead)

            if triage.is_spam:
                self._log(sid, stage, "spam", triage.reason, lead, raw=raw)
                return Outcome(sid, "spam", tag="", detail=triage.reason)

            if match and match.hard:
                return self._handle_duplicate(lead, match, triage, raw)

            stage = "write"
            flags = list(lead.flags) + list(triage.flags)
            if match:  # soft: same person+company, different email
                flags.append(f"possible_duplicate_of_{match.lead_id}")
            lead_id = self.store.next_lead_id()
            row = {
                "lead_id": lead_id, "name": safe_cell(lead.name), "email": lead.email,
                "company": safe_cell(lead.company), "source": lead.source, "tag": triage.tag,
                "created_at": lead.created_date, "phone": lead.phone, "message": safe_cell(lead.message),
                "draft_reply": triage.draft_reply, "tag_reason": triage.reason, "flags": ";".join(flags),
                "source_id": sid, "received_at": lead.received_at, "processed_at": now_iso(),
                "triage_mode": triage.mode,
            }
            self.store.add_row(row)
            self.index.add(lead_id, lead.email, lead.phone, lead.name, lead.company)
            self._log(sid, stage, "added", ";".join(flags), lead, lead_id=lead_id)
            if match:
                self._log(sid, "dedupe", "warning", f"possible duplicate of {match.lead_id} (same name+company, different email)",
                          lead, lead_id=lead_id, matched=match.lead_id)

            notes = []
            if triage.tag == "Hot":
                stage = "notify"
                failed = self._notify(row, sid)
                notes.append("notified" if not failed else f"notify failed: {','.join(failed)}")
            return Outcome(sid, "added", lead_id, triage.tag, detail=triage.reason, notes=notes)

        except Exception as exc:  # noqa: BLE001  (one bad lead must not stop the batch)
            reason = f"{type(exc).__name__}: {exc}"
            self._log(sid or "(unknown)", stage, "error", reason + " | " + traceback.format_exc(limit=2).replace("\n", " "),
                      lead, raw=raw)
            return Outcome(sid or "(unknown)", "error", detail=f"stage={stage}: {reason}")

    # ------------------------------------------------------------------ duplicates
    def _handle_duplicate(self, lead: Lead, match, triage, raw) -> Outcome:
        """A duplicate is never silently dropped: the new message may carry buying intent.

        We do not create a second row. We log it, and if the new message is hotter than the tag on the
        existing row we upgrade that row (never downgrade) and notify.
        """
        existing = self.store.get_row(match.lead_id)
        old = existing["tag"] if existing else ""
        notes = []
        self._log(lead.source_id, "dedupe", "duplicate",
                  f"same {match.kind} as {match.lead_id}; new message tagged {triage.tag}: {lead.message[:120]}",
                  lead, matched=match.lead_id, raw=raw)
        if existing and not triage.injection and RANK.get(triage.tag, 0) > RANK.get(old, 0):
            flags = (existing.get("flags", "") + ";" if existing.get("flags") else "") + \
                    f"escalated_{old or 'none'}_to_{triage.tag}_by_{lead.source_id}"
            self.store.update_row(match.lead_id, tag=triage.tag, flags=flags,
                                  tag_reason=f"Upgraded by follow-up {lead.source_id}: {triage.reason}")
            self._log(lead.source_id, "dedupe", "escalated", f"{match.lead_id}: {old} -> {triage.tag}",
                      lead, lead_id=match.lead_id, matched=match.lead_id)
            notes.append(f"escalated {old} -> {triage.tag}")
            if triage.tag == "Hot":
                failed = self._notify(self.store.get_row(match.lead_id), lead.source_id,
                                      note=f"Upgraded by follow-up {lead.source_id}: \"{lead.message[:160]}\"")
                notes.append("notified" if not failed else f"notify failed: {','.join(failed)}")
        return Outcome(lead.source_id, "duplicate", match.lead_id, triage.tag,
                       detail=f"duplicate of {match.lead_id} (matched on {match.kind})", notes=notes)
