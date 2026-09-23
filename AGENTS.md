# Working Agreements

## Default autonomy

Routine, reversible, low-risk engineering operations should proceed without asking the user.

Do not ask for approval merely to:

- inspect repository files
- inspect Git status / diff / log / branches
- read logs
- read/query SQLite databases
- make isolated database copies for testing
- run tests, linters, type checks
- inspect configuration files that do not expose secrets
- inspect Docker containers/images/logs
- start/stop/rebuild/recreate local Docker containers for development or testing
- run Docker Compose locally
- execute routine debugging commands

## Trusted Raspberry Pi

The following SSH target is explicitly trusted:

```text
ssh adely@100.105.152.97
```

Connecting to this host does not require asking the user.

Routine commands on this host may be executed without additional confirmation, including filesystem inspection, Git operations that do not rewrite history, reading logs, SQLite queries, Docker / Docker Compose inspection, container start / stop / rebuild / restart, test execution, application inspection, and routine deployment/debugging operations for this project.

Do not repeatedly ask for approval simply because SSH is involved.

## Local Docker

Local Docker / Docker Compose is explicitly authorized for development and testing.

Codex may freely use Docker and Docker Compose commands, including temporary containers and networks for testing.

## Database

Reading existing databases is authorized. Writing to temporary/test database copies is authorized.

Ask before destructive mutation of production data, dropping production tables, deleting persistent production databases, or destructive migrations without a clear rollback path.

## External APIs / cost

Small and clearly bounded smoke tests may be performed when needed.

Ask before bulk external API usage, repeated paid API workloads, materially expensive OpenAI/Web Search use, or workloads expected to produce dozens of paid API calls.

## External side effects

Ask before sending production email / DM / contact-form messages, intentionally sending production Discord/webhooks unless explicitly part of an authorized live test, credential rotation, authentication/security policy changes, force push / history rewrite, deleting persistent Docker volumes, or other irreversible destructive operations.

## General principle

If an operation is local or on the explicitly trusted Raspberry Pi, routine, reversible, inexpensive, and does not create an external communication side effect, prefer proceeding without asking.

## Project-level RPI5 autonomy

For this Sales Agent project, the user pre-authorizes autonomous execution on
the trusted RPI5 for work explicitly requested in the current task. Do not
pause for separate approval between routine steps. In particular, Codex may
freely SSH to the Pi and inspect or modify project files, update non-secret
configuration, query the project SQLite database, inspect logs, run tests,
build/rebuild/restart Docker Compose services, run offline replays, and perform
Git operations including commit and push when they are part of the requested
change.

The same autonomy applies to bounded project API calls that are explicitly
requested, and to Discord webhook sends when the task explicitly requests a
live test or replay. Do not start an unrequested daily run, broad Web Search,
or materially larger paid workload. Do not delete persistent data, drop
production tables, remove Docker volumes, rotate credentials, or send outreach
messages without explicit task scope.

This RPI5 authorization covers the complete sequence of a requested operation:
SSH, file transfer, Docker build/test, application execution, log/database
verification, and final report delivery. Do not request permission again for
each individual command in that sequence.
