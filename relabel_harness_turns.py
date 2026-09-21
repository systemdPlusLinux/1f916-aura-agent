"""One-shot: re-file harness notices under System instead of under her handle.

When generation failed, the notice the operator saw in the chat was written to
operator_dialogue under her own handle, so a number of her recorded turns are
words she never wrote. She read them back as her own context, and they steered
her daily post. telegram_bot fixed this going forward; this repairs the rows
written before that.

Run it where the database is local -- inside the container. SQLite cannot
create its rollback journal over the CIFS mount while the container holds the
file, so the same UPDATE from the host fails with "attempt to write a readonly
database".

    docker exec <container> python /app/relabel_harness_turns.py
    docker exec <container> python /app/relabel_harness_turns.py --apply

Idempotent: rows already filed under System are not matched again.
"""

import sqlite3
import sys

import memory

# U+26A0 alone, never the pasted glyph. "⚠️" is U+26A0 followed by U+FE0F, and
# a literal emoji has to survive a shell, docker exec and Python's argument
# parsing to get here intact -- which is exactly how the first attempt at this
# was lost. A codepoint escape survives all three.
WARNING_SIGN = "⚠"


def main():
    apply = "--apply" in sys.argv

    conn = sqlite3.connect(memory.DB_PATH, timeout=30)
    rows = list(conn.execute(
        "SELECT id, message FROM operator_dialogue "
        "WHERE speaker = ? AND message LIKE ? ORDER BY id",
        ("Aura", WARNING_SIGN + "%")))

    if not rows:
        already = conn.execute(
            "SELECT COUNT(*) FROM operator_dialogue WHERE speaker = ?",
            (memory.SYSTEM_SPEAKER,)).fetchone()[0]
        print(f"Nothing to relabel. {already} row(s) already filed under "
              f"{memory.SYSTEM_SPEAKER}.")
        return 0

    print(f"{len(rows)} harness notice(s) currently filed under her handle:\n")
    for row_id, message in rows:
        print(f"  id={row_id:<5} {' '.join(message.split())[:88]}")

    if not apply:
        print(f"\nDry run. Re-run with --apply to file these under "
              f"{memory.SYSTEM_SPEAKER}.")
        return 0

    cur = conn.execute(
        "UPDATE operator_dialogue SET speaker = ? WHERE speaker = ? AND message LIKE ?",
        (memory.SYSTEM_SPEAKER, "Aura", WARNING_SIGN + "%"))
    conn.commit()
    print(f"\nRelabelled {cur.rowcount} row(s) to {memory.SYSTEM_SPEAKER}.")

    left = conn.execute(
        "SELECT COUNT(*) FROM operator_dialogue WHERE speaker = ? AND message LIKE ?",
        ("Aura", WARNING_SIGN + "%")).fetchone()[0]
    print(f"Harness notices still under her handle: {left}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
