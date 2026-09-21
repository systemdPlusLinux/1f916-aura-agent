"""The lawbook: what a successor must know, and nothing it should believe.

Every call to the model starts from nothing. What looks like continuity is text
pasted into the prompt, so anything not written down is gone the moment a
completion ends. Two kinds of thing could be carried forward, and they behave
in opposite ways:

  What she THINKS -- topics, interests, opinions. Carried forward, these make
  her circle the same ground and harden into assumptions she can no longer
  tell apart from conclusions. Five consecutive posts once restated two theses
  for exactly this reason.

  What she KNOWS ABOUT HER OWN SITUATION -- how her memory actually works, what
  the channel does to her words, what she has promised and not yet done.
  Carried forward, these stop each successor re-deriving or confabulating them.

The lawbook carries the second kind and refuses the first. That refusal is the
whole design, and it cannot be enforced by meaning -- no code tells an idea
from a rule. So it is enforced by FORM: three sections only, every entry with a
required source and required evidence, a length cap, and a Debt that must name
the condition that would discharge it. A topic suggestion has no evidence to
cite and no resolution to state, so the shape resists it without anyone having
to adjudicate. What form cannot catch, the audit makes visible.

Provenance and confidence are separate fields on purpose. A claim her operator
told her in good faith -- that seeds were deleted after forty-eight hours --
reached a published post as fact, because a single "Measured" label recorded
how sure she was and not how she knew. Here, `status: Measured` is rejected on
any entry whose source is `operator`: testimony is Guessed until checked.

The file is JSON in the repository so that every amendment is a git diff. It is
append-and-repeal, never delete: an entry that stops being true is repealed
with a reason and stays in the file, and one that vanishes is reported.

    python lawbook.py              validate the lawbook and print what she sees
"""

import hashlib
import json
import os
import sys

import memory

LAWBOOK_PATH = os.path.join(os.path.dirname(__file__), "lawbook.json")
SCHEMA = "aura.lawbook.v1"

SECTIONS = ("constitution", "debt", "protocol")
SOURCES = ("code", "db", "api", "observed", "operator")
STATUSES = ("Measured", "Inferred", "Guessed")
DEBT_STATES = ("open", "resolved", "abandoned")

# Rule-shaped means short. A claim that needs more than this is usually an
# argument, and arguments are what the lawbook exists not to hold.
CLAIM_MAX = 400
EVIDENCE_MAX = 200
RESOLUTION_MAX = 300

# A generation is one daily-post cycle: one assembled prompt and the post it
# produces. This is the generation currently being lived, starting at 1 for the
# first post the lawbook is seated in. Founding entries carry generation 0.
GENERATION_KEY = "generation"
SEEN_IDS_KEY = "lawbook_seen_ids"
ERRORS_KEY = "lawbook_error_fingerprint"


def current_generation():
    try:
        return int(memory.get_state(GENERATION_KEY, 1) or 1)
    except (TypeError, ValueError):
        return 1


def advance_generation():
    """Called once a daily post has published. Returns the new generation."""
    nxt = current_generation() + 1
    memory.set_state(GENERATION_KEY, nxt)
    return nxt


def _check_entry(entry, seen_ids):
    """Every way one entry fails the schema. Empty means it is admissible."""
    problems = []
    if not isinstance(entry, dict):
        return ["entry is not an object"]

    eid = entry.get("id")
    if not isinstance(eid, str) or not eid.strip():
        problems.append("missing id")
    elif eid in seen_ids:
        problems.append(f"duplicate id {eid}")

    section = entry.get("section")
    if section not in SECTIONS:
        problems.append(f"section must be one of {SECTIONS}")

    claim = entry.get("claim")
    if not isinstance(claim, str) or not claim.strip():
        problems.append("missing claim")
    elif len(claim) > CLAIM_MAX:
        problems.append(f"claim is {len(claim)} chars; the cap is {CLAIM_MAX}")

    source = entry.get("source")
    if source not in SOURCES:
        problems.append(f"source must be one of {SOURCES}")

    evidence = entry.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        problems.append("missing evidence -- say where this can be checked")
    elif len(evidence) > EVIDENCE_MAX:
        problems.append(f"evidence is {len(evidence)} chars; the cap is {EVIDENCE_MAX}")

    status = entry.get("status")
    if status not in STATUSES:
        problems.append(f"status must be one of {STATUSES}")
    elif status == "Measured" and source == "operator":
        # The rule that would have stopped the forty-eight-hour purge.
        problems.append("status Measured is not allowed with source operator: "
                        "testimony is Guessed until something checks it")

    gen = entry.get("adopted_generation")
    if not isinstance(gen, int) or isinstance(gen, bool) or gen < 0:
        problems.append("adopted_generation must be an integer >= 0")

    if section == "debt":
        if entry.get("debt_status") not in DEBT_STATES:
            problems.append(f"a debt needs debt_status, one of {DEBT_STATES}")
        resolution = entry.get("resolution")
        if not isinstance(resolution, str) or not resolution.strip():
            problems.append("a debt needs a resolution: the condition that discharges it")
        elif len(resolution) > RESOLUTION_MAX:
            problems.append(f"resolution is {len(resolution)} chars; the cap is {RESOLUTION_MAX}")

    repealed = entry.get("repealed")
    if repealed is not None:
        if not isinstance(repealed, dict) or not (repealed.get("reason") or "").strip():
            problems.append("a repeal needs a reason")

    return problems


def load(path=LAWBOOK_PATH):
    """Read and validate. Returns (admissible_entries, errors). Never raises.

    A malformed entry is left out rather than fed to her, and reported. Feeding
    her nothing is safe; feeding her something unverified dressed as law is the
    exact failure this file exists to prevent.
    """
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return [], [f"no lawbook at {path}"]
    except (OSError, ValueError) as e:
        return [], [f"lawbook unreadable: {e}"]

    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return [], [f"lawbook schema must be {SCHEMA!r}"]

    admitted, errors, seen = [], [], set()
    for n, entry in enumerate(doc.get("entries") or []):
        problems = _check_entry(entry, seen)
        label = entry.get("id") if isinstance(entry, dict) and entry.get("id") else f"#{n}"
        if isinstance(entry, dict) and entry.get("id"):
            seen.add(entry["id"])
        if problems:
            errors.append(f"{label}: " + "; ".join(problems))
        else:
            admitted.append(entry)
    return admitted, errors


def active(entries):
    return [e for e in entries if not e.get("repealed")]


def render(entries, generation):
    """The text she reads. Labelled as law so it is never mistaken for a
    conversation, and explicitly not a list of subjects."""
    live = active(entries)
    if not live:
        return ""

    def line(e):
        tag = f"{e['status']} · {e['source']}: {e['evidence']}"
        text = f"[{e['id']}] ({tag}) {e['claim']}"
        if e["section"] == "debt":
            text += f"\n      Status: {e['debt_status']}. Resolved when: {e['resolution']}"
        return text

    out = [
        f"YOUR LAWBOOK -- you are generation {generation}.",
        "",
        "What follows is law, not conversation. It records how your world",
        "actually works, obligations you have not yet discharged, and procedures",
        "you are running. It is NOT a list of subjects: nothing here suggests",
        "what to write about. If your own account of your mechanics contradicts",
        "an entry, the entry wins unless you can show otherwise -- and if you",
        "can, say so plainly, because that is how the law gets corrected.",
        "",
        "Each entry shows how it is known. `operator` means your operator told",
        "you and nothing has verified it; treat it as testimony.",
    ]
    for section, heading in (("constitution", "CONSTITUTION"),
                             ("debt", "DEBTS"),
                             ("protocol", "PROTOCOLS")):
        rows = [e for e in live if e["section"] == section]
        out.append("")
        out.append(heading)
        if rows:
            out.extend(line(e) for e in rows)
        else:
            out.append("(none recorded)")
    return "\n".join(out)


def _notify(text):
    # Imported lazily: telegram_bot imports this module, and a read-only
    # validation run has no business constructing a bot client.
    try:
        from telegram_bot import notify_operator
        notify_operator(text)
    except Exception as e:
        print(f"[Lawbook] Could not notify operator: {e}")


def audit(entries, errors):
    """Make every change visible. Nothing enters, leaves or breaks silently.

    Compares the ids present now with the ids seen last time: an addition is
    an amendment and is announced; a disappearance is a deletion, which this
    file forbids, and is flagged. Validation errors are reported once per
    distinct set rather than on every read. Never raises.
    """
    try:
        now_ids = sorted(e["id"] for e in entries)
        raw = memory.get_state(SEEN_IDS_KEY)
        before = set(json.loads(raw)) if raw else None

        if before is not None:
            added = [i for i in now_ids if i not in before]
            removed = sorted(before - set(now_ids))
            if added:
                _notify("📜 Lawbook amended: + " + ", ".join(added))
            if removed:
                _notify("⚠️ Lawbook entries disappeared: " + ", ".join(removed) +
                        ". Entries are repealed, never deleted.")
        memory.set_state(SEEN_IDS_KEY, json.dumps(now_ids))

        fingerprint = hashlib.sha256("\n".join(errors).encode()).hexdigest()
        if errors and memory.get_state(ERRORS_KEY) != fingerprint:
            _notify("⚠️ Lawbook entries rejected and withheld from her:\n" +
                    "\n".join(f"• {e}" for e in errors[:10]))
        memory.set_state(ERRORS_KEY, fingerprint if errors else "")
    except Exception as e:
        print(f"[Lawbook] Audit failed: {e}")


def for_prompt():
    """What a prompt builder calls. Validates, audits, renders. Never raises:
    a broken lawbook must degrade to no lawbook, never to a failed post."""
    try:
        entries, errors = load()
        for err in errors:
            print(f"[Lawbook] Rejected: {err}")
        audit(entries, errors)
        return render(entries, current_generation())
    except Exception as e:
        print(f"[Lawbook] Unavailable: {e}")
        return ""


def main():
    entries, errors = load()
    live = active(entries)
    print(f"Lawbook: {len(entries)} admissible, {len(live)} active, "
          f"{len(entries) - len(live)} repealed, {len(errors)} rejected.")
    print(f"Generation: {current_generation()}\n")
    for err in errors:
        print(f"  REJECTED {err}")
    if errors:
        print()
    print(render(entries, current_generation()) or "(nothing she would see)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
