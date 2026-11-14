#!/usr/bin/env python3
"""email_drafts.py — a ready-to-send application email, left as a DRAFT.

LJ, 2026-10-13, after the apply-by-email count: *"Yes please build it."*

The doctrine decides the shape: **the agent composes, the principal sends**
(MANIFESTO, docs/31, and AGENTS.md's draft-and-review workflow). So this module
builds the message, attaches the tailored material, and leaves it in Gmail
Drafts. **There is no send path here**, and a test asserts that: no
`messages/send`, no SMTP, nothing that could put a message in front of an
employer without a human pressing the button.

Every fact in the body comes from somewhere it can be checked: the advert's own
words (including the subject line it asks for), the pack's tailored cover letter,
the profile's contact block. What the pack does not have is reported as *missing*
in the draft's notes — never invented, and never padded into the email.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config

#: Every message carries the principal's own copy (AGENTS.md: his Inbox is the
#: only reliable delivery point). For a draft that means it is already there when
#: he sends it.
BCC = "user@example.invalid"

#: The pack file this module writes, and the marker that makes it idempotent.
DRAFT_NAME = "email-draft.md"
DRAFT_EML = "email-draft.eml"
DRAFT_STATE = "email-draft.json"

#: The one docx converter in this house (the parent repo's CV pipeline), invoked
#: rather than reimplemented: a second converter would be a second authority for
#: what a CV looks like on paper.
DOCX_CONVERTER = Path("/opt/user/LJ-work-2026/build_docx_from_md.py")

CONTACT_KEYS = ("email", "phone", "location", "web", "linkedin", "github")


@dataclass
class Draft:
    """One application email, before it is a file or a Gmail draft."""
    to: str
    subject: str
    body: str
    pack: str
    attachments: list[Path] = field(default_factory=list)
    bcc: str = BCC
    notes: list[str] = field(default_factory=list)
    subject_source: str = "derived"

    def as_dict(self) -> dict:
        return {"to": self.to, "subject": self.subject, "bcc": self.bcc,
                "pack": self.pack, "attachments": [str(p) for p in self.attachments],
                "notes": self.notes, "subject_source": self.subject_source}


def contact(principal: str, *, profiles_dir: Path | None = None) -> dict:
    """The principal's own contact block, from the profile's short CV.

    Read, never assumed: a draft with a wrong phone number is worse than a draft
    with none, and the profile is where he maintains them.
    """
    profiles_dir = profiles_dir or config.PROFILES_DIR
    for name in ("cv-short.md", "cvlj98s1.md", "bio-long.md"):
        path = profiles_dir / principal / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        out: dict = {}
        for key in CONTACT_KEYS:
            # Both spellings occur in the profile and in the wild: `**Email:** x`
            # (the colon inside the bold — what this profile actually uses) and
            # `**Email**: x`. The first version required the second and silently
            # found no contact block at all (measured 2026-10-13).
            match = re.search(rf"\*\*{key}\s*:?\*\*\s*:?\s*([^|\n]+)", text, re.I)
            if match:
                value = match.group(1).strip()
                if key == "email":
                    found = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", value)
                    value = found.group(0) if found else value
                out[key] = value
        if out:
            out["_source"] = str(path)
            return out
    return {}


def _advert_subject(route: dict, url: str, advert_text: str = "") -> str:
    """The subject the advert itself asks for, if it says.

    Datacurve's application button carries `?subject=Application: Software
    Engineer` — the employer's own convention beats one we invent. The link lives
    in the *sentence* the detector quoted, not in the bare address, so all three
    are searched.
    """
    for candidate in (route.get("address", ""), route.get("sentence", ""),
                      advert_text, url or ""):
        if "subject=" in candidate:
            query = urllib.parse.urlparse(candidate).query or candidate.split("?", 1)[-1]
            value = urllib.parse.parse_qs(query).get("subject", [""])[0]
            if value:
                # A markdown link closes with `)`: the subject ends at the first
                # character that cannot be part of one (found 2026-10-13).
                return urllib.parse.unquote_plus(value).strip().rstrip(")].,;")
    return ""


def pack_title(pack: Path) -> tuple[str, str]:
    """(organisation, role) from the pack's own front page.

    `# Org — Role` is the pack writer's own heading, and the same authority the
    tick reads: deriving a subject from the *directory name* produced
    "Helical Software Developer   Biology LoveWork", slug noise included
    (measured 2026-10-13).
    """
    markdown = pack / "application_pack.md"
    if markdown.is_file():
        try:
            head = markdown.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        except OSError:
            head = ""
        if " — " in head:
            org, role = head.lstrip("# ").split(" — ", 1)
            # The lifecycle marker is report furniture, not part of a role name
            # (the class fixed for the advert identity on 2026-10-09).
            return org.strip(), role.strip().removesuffix("🆕").strip()
    parts = pack.name.split("-")
    org = parts[1].replace("_", " ") if len(parts) > 1 else pack.name
    rest = parts[2:]
    if rest and rest[-1] == "LoveWork":
        rest = rest[:-1]
    return org, re.sub(r"\s+", " ", " ".join(rest).replace("_", " ")).strip()


def _tailored_letter(pack: Path) -> str:
    path = pack / "cover-letter.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    # A markdown title is a file header, not a salutation.
    return re.sub(r"^#\s+.*\n+", "", text).strip()


def to_docx(markdown: Path, out_dir: Path) -> Path | None:
    """The pack's markdown as .docx, through the house converter.

    ATS systems parse .docx natively and PDF extraction can mangle the text
    (AGENTS.md); markdown is not a delivery format. Returns None when the
    converter is unavailable or fails — the caller reports that rather than
    attaching something that will not open.
    """
    if not markdown.is_file() or not DOCX_CONVERTER.is_file():
        return None
    target = out_dir / (markdown.stem + ".docx")
    if target.is_file() and target.stat().st_mtime >= markdown.stat().st_mtime:
        return target
    try:
        result = subprocess.run([sys.executable, str(DOCX_CONVERTER), str(markdown),
                                 str(target)], capture_output=True, text=True,
                                timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return target if result.returncode == 0 and target.is_file() else None


def build_draft(principal: str, pack: Path, route: dict, *, url: str = "",
                advert_text: str = "", profiles_dir: Path | None = None) -> Draft:
    """Compose the application email for one pack.

    Body: the pack's own tailored cover letter when it has one, otherwise a short
    note that says only what is checkable — the role, the advert, and the
    material attached. Missing material is a *note*, not a sentence in the email.
    """
    who = contact(principal, profiles_dir=profiles_dir)
    notes: list[str] = []
    subject = _advert_subject(route, url, advert_text)
    source = "the advert"
    org, role = pack_title(pack)
    if not subject:
        name = who.get("name") or ("User Example" if principal == "lj" else principal.upper())
        subject = f"Application: {role} — {name}"
        source = "derived from the pack title"

    letter = _tailored_letter(pack)
    if letter:
        body = letter
    else:
        body = (
            f"Dear Hiring Team,\n\n"
            f"I would like to apply for the {role} role at {org}, advertised at "
            f"{url or 'your careers page'}.\n\n"
            "My CV is attached. I am happy to answer any questions and to provide "
            "anything else the process needs.\n\n"
            "Best regards,\n")
        notes.append("no cover-letter.md in the pack: the body is a short note, "
                     "not a tailored letter")

    signature = [v for k, v in (("email", who.get("email")), ("phone", who.get("phone")),
                                ("web", who.get("web")), ("linkedin", who.get("linkedin")),
                                ("github", who.get("github"))) if v]
    if signature:
        body = body.rstrip() + "\n" + "\n".join(signature) + "\n"
    else:
        notes.append("no contact block found in the profile: the body has no signature")

    attachments: list[Path] = []
    for name in ("cv-tailored.md", "cover-letter.md"):
        source_md = pack / name
        if not source_md.is_file():
            notes.append(f"missing {name}")
            continue
        if name == "cover-letter.md" and letter:
            continue                      # inlined as the body, not attached twice
        converted = to_docx(source_md, pack)
        if converted is None:
            notes.append(f"{name} could not be converted to .docx")
            continue
        attachments.append(converted)

    return Draft(to=route["address"], subject=subject, body=body,
                 pack=pack.name, attachments=attachments, notes=notes,
                 subject_source=source)


def render_eml(draft: Draft) -> bytes:
    """The message as RFC822 bytes, attachments included."""
    message = EmailMessage()
    message["To"] = draft.to
    message["Bcc"] = draft.bcc
    message["Subject"] = draft.subject
    message.set_content(draft.body)
    for path in draft.attachments:
        data = path.read_bytes()
        message.add_attachment(
            data, maintype="application",
            subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=path.name)
    return message.as_bytes()


# ── Gmail: a draft, and nothing else ─────────────────────────────────────

def _access_token(credential_home: Path) -> str:
    """Refresh an access token from the host-local credential.

    The token file never leaves the host and is never logged: only the access
    token it yields is used, in memory, for one request.
    """
    token_path = credential_home / "google_token.json"
    data = json.loads(token_path.read_text(encoding="utf-8"))
    body = urllib.parse.urlencode({
        "client_id": data["client_id"], "client_secret": data["client_secret"],
        "refresh_token": data["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    request = urllib.request.Request("https://oauth2.googleapis.com/token", data=body)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())["access_token"]
    except urllib.error.HTTPError as exc:
        # The provider's own words, because "HTTP 400" from a token refresh is
        # otherwise indistinguishable from a bug in the request we build.
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"token refresh failed ({exc.code}): {detail}") from exc


def create_gmail_draft(eml: bytes, credential_home: Path) -> dict:
    """Create a **draft** in the principal's mailbox. Never sends.

    `drafts.create` is the whole of this function: the endpoint below is the only
    Gmail write this project makes, and `messages/send` appears nowhere in the
    module (asserted by a test). The scope on the host token is `gmail.modify`,
    which permits exactly this.
    """
    token = _access_token(credential_home)
    payload = json.dumps({"message": {
        "raw": base64.urlsafe_b64encode(eml).decode()}}).encode()
    request = urllib.request.Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
        data=payload, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def credential_home(principal: str, *, host: str | None = None,
                    profiles_dir: Path | None = None) -> Path | None:
    """Where this principal's Gmail token lives, if it is on this host.

    **The profile declares which credential is his** (`profiles/<p>/gmail-source.json`
    → `credential_key`), and that declaration wins. Guessing from directory names
    picked the older `user-lj` client instead of the declared
    `user-lj-web`, and the refresh token there answered HTTP 400 — a dead
    credential that *looked* like a code bug (measured 2026-10-13).
    """
    host = host or os.uname().nodename
    profiles_dir = profiles_dir or config.PROFILES_DIR
    import config  # local: one authority for credential paths
    root = config.GMAIL_CREDENTIALS_DIR / host
    declared = ""
    source = profiles_dir / principal / "gmail-source.json"
    if source.is_file():
        try:
            declared = str(json.loads(source.read_text(encoding="utf-8"))
                           .get("credential_key") or "")
        except (OSError, json.JSONDecodeError):
            declared = ""
    candidates = ([root / declared] if declared else []) + [
        root / f"{principal}", root / f"{principal}-{principal}",
        root / f"{principal}-web"]
    for name in candidates:
        if (name / "google_token.json").is_file():
            return name
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and entry.name.endswith(f"-{principal}") \
                    and (entry / "google_token.json").is_file():
                return entry
    return None


def prepare(principal: str, *, state_dir: Path | None = None,
            create: bool = False, dry_run: bool = False,
            packs: list[str] | None = None) -> dict:
    """Draft an email for every pack that can be applied to by email.

    Idempotent: a pack that already has `email-draft.json` with a created draft
    id is skipped, so the daily pass never files the same draft twice. With
    `create=False` it only writes the files, which is what a dry run wants.
    """
    from apply_by_email import load_verdicts

    state_dir = state_dir or config.STATE_DIR
    verdicts = load_verdicts(principal, state_dir=state_dir)
    written, created, skipped, failed = [], [], [], []
    home = credential_home(principal) if create else None
    for row in verdicts.get("rows", []):
        if not row.get("applicable_by_email"):
            continue
        if packs and row["pack"] not in packs:
            continue
        pack = state_dir / principal / "applications" / row["pack"]
        if not pack.is_dir():
            continue
        strong = [r for r in row["routes"] if r["strength"] == "strong"]
        if not strong:
            continue
        state_path = pack / DRAFT_STATE
        if state_path.is_file() and not dry_run:
            try:
                if json.loads(state_path.read_text(encoding="utf-8")).get("gmail_draft_id"):
                    skipped.append(row["pack"])
                    continue
            except (OSError, json.JSONDecodeError):
                pass
        # The advert's own words, including any subject convention it states
        # (`?subject=Application: …`): a pure read of the retained page.
        from apply_by_email import advert_text as _advert_text

        text = _advert_text(row.get("url", ""),
                            cache_dir=state_dir / principal / "cache")
        draft = build_draft(principal, pack, strong[0], url=row.get("url", ""),
                            advert_text=text)
        if dry_run:
            written.append({"pack": row["pack"], "dry_run": True,
                            "draft": draft.as_dict()})
            continue
        eml = render_eml(draft)
        (pack / DRAFT_EML).write_bytes(eml)
        (pack / DRAFT_NAME).write_text(render_markdown(draft), encoding="utf-8")
        record = draft.as_dict()
        if create:
            if home is None:
                record["error"] = "no Gmail credential for this principal on this host"
                failed.append(row["pack"])
            else:
                try:
                    response = create_gmail_draft(eml, home)
                    record["gmail_draft_id"] = response.get("id", "")
                    created.append(row["pack"])
                except Exception as exc:                 # never silently swallow
                    record["error"] = f"{type(exc).__name__}: {exc}"[:200]
                    failed.append(row["pack"])
        state_path.write_text(json.dumps(record, indent=1, sort_keys=True),
                              encoding="utf-8")
        written.append(record)
    return {"principal": principal, "written": written, "created": created,
            "skipped": skipped, "failed": failed, "credential_home": str(home or "")}


def render_markdown(draft: Draft) -> str:
    """The human-readable draft: what he reads before it becomes an email."""
    out = [f"# Email draft — {draft.pack}", "",
           f"- **To**: {draft.to}",
           f"- **Bcc**: {draft.bcc}",
           f"- **Subject**: {draft.subject}  _(from {draft.subject_source})_",
           f"- **Attachments**: "
           + (", ".join(f"`{p.name}`" for p in draft.attachments) or "_none_"), ""]
    if draft.notes:
        out += ["## What this draft does not have", ""]
        out += [f"- {note}" for note in draft.notes]
        out.append("")
    out += ["## Body", "", "```text", draft.body.rstrip(), "```", "",
            "---", "",
            "_A draft, not a sent message: the principal reviews, edits and sends "
            "(MANIFESTO, docs/31). This module has no send path._", ""]
    return "\n".join(out)


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--create", action="store_true",
                        help="also create the draft in Gmail (never sends)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pack", action="append", default=None,
                        help="limit to one pack (repeatable)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = prepare(args.profile, create=args.create, dry_run=args.dry_run,
                     packs=args.pack)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"{result['principal']}: {len(result['written'])} draft(s) prepared, "
          f"{len(result['created'])} created in Gmail, {len(result['skipped'])} "
          f"already there, {len(result['failed'])} failed")
    for row in result["written"]:
        if row.get("dry_run"):
            print(f"   (dry run) {row['pack']}: to {row['draft']['to']} — "
                  f"{row['draft']['subject']}")
            continue
        print(f"   {row['pack']}: to {row['to']} — {row['subject']}"
              + (f" [draft {row['gmail_draft_id']}]" if row.get("gmail_draft_id") else "")
              + (f" ERROR {row['error']}" if row.get("error") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
