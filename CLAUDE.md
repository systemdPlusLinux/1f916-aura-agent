# Aura -- working notes for Claude Code

**Start by reading `NEXT.md`.** It holds the planned work and open items.

Rules learned the hard way:

- **This folder is the live deployment.** It is mapped to `/app` in the
  container. A `.py` edit takes effect on the next restart. `lawbook.json` is
  re-read on every prompt, so a lawbook edit is live immediately: land it with
  the restart that makes it true, never ahead of it.
- **Never test against the live database.** It sits on a CIFS mount, where
  writes from this host fail with "attempt to write a readonly database", and
  importing `memory` creates any missing tables. Copy `aura_memory.db` into the
  scratchpad and set `AURA_DB_PATH` to the copy. Stub `client.api_post`,
  `send_telegram_message` and `notify_operator` in any test that could write.
- **Never import `run_loop` in a test.** It starts the agent.
- **Dependencies are not installed on this host.** Build a venv with
  `requests==2.34.2` and `python-dotenv==1.2.3`, or run inside the container.
- **Deployment is through the Unraid Docker UI.** The operator restarts it
  there. A change to `requirements.txt` needs an image rebuild.
- **Commit each change on its own. Push only when asked.** The GitHub repo is
  public, so scan for secrets before pushing.
