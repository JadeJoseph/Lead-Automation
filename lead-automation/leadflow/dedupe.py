"""Step 2: duplicate detection against the sheet AND against leads already seen in this batch.

Hard match  (lead is a duplicate): same canonical email, or same phone.
Soft match  (flag only, lead is kept): same normalised name + company but a different email.
The index is updated after every accepted lead, which is what catches within-batch duplicates.
"""
from __future__ import annotations

from dataclasses import dataclass

from .clean import canonical_email, company_key, name_key


@dataclass
class Match:
    kind: str  # email | phone | name_company
    lead_id: str

    @property
    def hard(self) -> bool:
        return self.kind in ("email", "phone")


class DedupeIndex:
    def __init__(self):
        self.by_email: dict[str, str] = {}
        self.by_phone: dict[str, str] = {}
        self.by_name_company: dict[tuple[str, str], str] = {}

    def add(self, lead_id: str, email: str, phone: str, name: str, company: str) -> None:
        ce = canonical_email(email)
        if ce:
            self.by_email.setdefault(ce, lead_id)
        if phone:
            self.by_phone.setdefault(phone, lead_id)
        nk, ck = name_key(name), company_key(company)
        if nk and ck:
            self.by_name_company.setdefault((nk, ck), lead_id)

    def find(self, email: str, phone: str, name: str, company: str) -> Match | None:
        ce = canonical_email(email)
        if ce and ce in self.by_email:
            return Match("email", self.by_email[ce])
        if phone and phone in self.by_phone:
            return Match("phone", self.by_phone[phone])
        nk, ck = name_key(name), company_key(company)
        if nk and ck and (nk, ck) in self.by_name_company:
            return Match("name_company", self.by_name_company[(nk, ck)])
        return None
