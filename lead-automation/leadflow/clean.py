"""Step 1: normalise a raw form submission into a consistent Lead.

Policy: repair only what is mechanically unambiguous (whitespace, casing, a doubled "@",
phone formatting). Never *guess* at content (e.g. we do not "fix" ben@gmail into
ben@gmail.com). Every repair is recorded in `flags` so it is visible in the sheet.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@(?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+[a-z]{2,}$")
PHONE_RE = re.compile(r"(?<!\w)(\+?\d[\d\s().\-]{7,}\d)")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
WS_RE = re.compile(r"\s+")
MAX_MESSAGE = 2000

LOWER_PARTICLES = {"de", "da", "van", "von", "der", "den", "la", "le", "di", "del", "bin", "al"}
COMPANY_NOISE = {"and", "the", "ltd", "limited", "inc", "llc", "gmbh", "plc", "co", "company"}


@dataclass
class Lead:
    source_id: str
    received_at: str
    name: str
    email: str
    company: str
    message: str
    phone: str
    source: str = "website"
    flags: list = field(default_factory=list)
    invalid_email: str = ""  # the cleaned-but-invalid value, kept only for the error message
    raw: dict = field(default_factory=dict)

    @property
    def created_date(self) -> str:
        return self.received_at[:10]

    @property
    def first_name(self) -> str:
        return self.name.split(" ")[0] if self.name else ""


# ----------------------------------------------------------------------------- text helpers
def squash(value) -> str:
    """Strip control chars, NFC-normalise, collapse all whitespace runs to one space."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", CTRL_RE.sub("", str(value)))
    return WS_RE.sub(" ", text).strip()


def _case_token(tok: str, first: bool) -> str:
    for sep in ("'", "’"):
        if sep in tok:
            return sep.join(p[:1].upper() + p[1:].lower() for p in tok.split(sep))
    if "-" in tok:
        return "-".join(_case_token(p, True) for p in tok.split("-"))
    if tok.islower() or tok.isupper():
        low = tok.lower()
        if not first and low in LOWER_PARTICLES:
            return low
        if low.startswith("mc") and len(low) > 2:
            return "Mc" + low[2:].capitalize()
        return low.capitalize()
    return tok  # already mixed-case ("McDonald", "DeShawn"): trust the sender


def normalise_name(raw) -> str:
    text = squash(raw)
    return " ".join(_case_token(t, i == 0) for i, t in enumerate(text.split(" "))) if text else ""


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def name_key(name: str) -> str:
    tokens = re.findall(r"[a-z]+", _strip_accents(name).lower())
    return " ".join(sorted(tokens))


def company_key(company: str) -> str:
    text = _strip_accents(company).lower().replace("&", " and ")
    tokens = [t for t in re.findall(r"[a-z0-9]+", text) if t not in COMPANY_NOISE]
    return " ".join(tokens)


def safe_cell(text: str) -> str:
    """Neutralise spreadsheet formula injection in free text (=, +, -, @ at the start)."""
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


# ----------------------------------------------------------------------------- email
def clean_email(raw) -> tuple[str, str, list]:
    """Return (valid_email_or_empty, invalid_value, flags)."""
    if raw is None or not str(raw).strip():
        return "", "", ["email_missing"]
    value = re.sub(r"^mailto:", "", str(raw).strip().lower())
    value = re.sub(r"\s+", "", value).strip("<>")
    flags = []
    if re.search(r"@{2,}", value):
        value = re.sub(r"@{2,}", "@", value)
        flags.append("email_repaired_double_at")
    if EMAIL_RE.match(value):
        return value, "", flags
    return "", value, flags + ["email_invalid"]


def canonical_email(email: str) -> str:
    """Dedupe key: lowercase, strip +tag, strip dots for Gmail. Display value is unchanged."""
    if not email or "@" not in email:
        return ""
    local, domain = email.lower().rsplit("@", 1)
    if domain == "googlemail.com":
        domain = "gmail.com"
    local = local.split("+", 1)[0]
    if domain == "gmail.com":
        local = local.replace(".", "")
    return f"{local}@{domain}" if local else ""


# ----------------------------------------------------------------------------- phone
def normalise_phone(raw: str, region: str = "GB") -> str:
    """Return E.164 or '' if it does not look like a real number. Stdlib only, UK-aware."""
    s = (raw or "").strip()
    digits = re.sub(r"\D", "", s)
    if not 9 <= len(digits) <= 15:
        return ""
    if s.startswith("+"):
        num = digits
    elif s.startswith("00"):
        num = digits[2:]
    elif digits.startswith("0") and region == "GB":
        num = "44" + digits[1:]
    else:
        return ""
    if num.startswith("440"):  # "+44 (0)7700..." style
        num = "44" + num[3:]
    if num.startswith("44") and len(num) != 12:
        return ""
    return "+" + num if 8 <= len(num) <= 15 else ""


def extract_phone(text: str, region: str = "GB") -> str:
    for match in PHONE_RE.finditer(text or ""):
        phone = normalise_phone(match.group(1), region)
        if phone:
            return phone
    return ""


# ----------------------------------------------------------------------------- lead
def _parse_received(value) -> tuple[str, list]:
    text = squash(value)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M"), []
        except ValueError:
            continue
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), ["received_at_invalid_used_now"]


def clean_lead(raw, region: str = "GB") -> Lead:
    if not isinstance(raw, dict):
        raise ValueError(f"payload is {type(raw).__name__}, expected an object")
    source_id = squash(raw.get("id"))
    if not source_id:
        raise ValueError("payload has no 'id'")

    flags: list = []
    received_at, f = _parse_received(raw.get("received_at"))
    flags += f

    name = normalise_name(raw.get("name"))
    if not name:
        flags.append("name_missing")
    company = squash(raw.get("company"))
    if not company:
        flags.append("company_missing")

    message = squash(raw.get("message"))
    if not message:
        flags.append("message_empty")
    if len(message) > MAX_MESSAGE:
        message = message[:MAX_MESSAGE]
        flags.append("message_truncated")

    email, invalid_email, f = clean_email(raw.get("email"))
    flags += f

    phone = normalise_phone(squash(raw.get("phone")), region) if raw.get("phone") else ""
    if not phone:
        phone = extract_phone(message, region)
        if phone:
            flags.append("phone_from_message")

    return Lead(
        source_id=source_id, received_at=received_at, name=name, email=email,
        company=company, message=message, phone=phone, flags=flags,
        invalid_email=invalid_email, raw=raw,
    )
