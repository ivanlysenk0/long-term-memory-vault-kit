# ltm-repo

<!-- ltm:start -->
## Long-term memory

This is NOT this project's memory and not the agent's MEMORY.md file. It is a separate
file-based knowledge store, shared by every project on this machine.

Memory directory: `/tmp/migen/vault`

How to approach it:
1. rules for working with the memory: `/tmp/migen/vault/CLAUDE.md`
2. entry point into the memory itself: `/tmp/migen/vault/00-global-home/master-index.md`
3. then follow the links from the master-index: `<project>/00-home/index.md`,
   `knowledge/decisions/`, `knowledge/patterns/`, `current-priorities.md`, `hot.md`,
   recent files in `<project>/sessions/`

When to use it: questions about decisions made earlier, architecture, the cause of a bug,
anything already discussed. Do not answer 'I do not know' before walking this path.

What to record: new connections, a synthesis across sources, a comparison of approaches,
the root cause of a bug, an architectural conclusion. Do not record a plain fact lookup.

At the end of a session: a log in `<project>/sessions/`, update `log.md`.
Health check: `python3 /tmp/migen/vault/scripts/ltm_doctor.py`
<!-- ltm:end -->
