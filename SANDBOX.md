# Sandboxing agent commands

Agents run shell commands, read and write files, and fetch URLs. What stops a
command from reaching the service's own credentials is the sandbox, and it is the
only thing that does — so it is worth knowing exactly what it guarantees and what
state this host is in.

Three mechanisms, deliberately separate:

1. **The sandbox is the security boundary.** It decides what a command *can* touch.
2. **Guardrails** (`app/orchestrator/tools.py`) are advisory regex over the command
   string. They put a human in the loop on `sudo`, `git push`, package installs and
   the like. A determined model can evade a regex, so this is not containment.
3. **Environment scrubbing is unconditional.** No `AGENT_HUB_*` variable and no
   resolved provider secret enters a command's environment, in either backend. It
   is an allowlist, not a denylist.

## Only `run` goes through the sandbox

This is the part that is easy to get wrong, so it is worth stating plainly: of the
four tools, **only `run` is executed inside the sandbox.** `read_file`, `write_file`
and `fetch` run in the service process, where no mount flag and no `--unshare-net`
applies to them. Left alone, they would be the easy way around the boundary — a
sandboxed job could read the provider key by asking for `read_file` instead of
`cat`.

`tools.refuse()` is what closes that, and it runs *before* the guardrails:

| | refused | gated | runs |
|---|---|---|---|
| `read_file` of a masked path | sandboxed | — | — |
| `read_file` elsewhere on the host | — | both | inside the workspace |
| `write_file` outside the workspace | sandboxed | unconfined | inside the workspace |
| `fetch` with the network off | both | — | — |
| `run` | never | on a guardrail match | otherwise |

Refused and gated are different in kind, and conflating them would be the bug.
A gate asks the operator about *one command*; approving a read of the provider key
in a job that was deliberately sandboxed would let a dialog revoke a security
setting. So the backend answers that one itself, the Commands tab labels it
`refused` rather than `declined` — nobody was asked — and no approval is created.

`run` is deliberately not second-guessed here: what a command may touch is the
sandbox's decision, and duplicating it in Python would mean two places deciding the
same thing, differently.

Masking is defined once, in `sandbox._masked_paths()`, and both the mount layout and
`refuse()` read it — so adding a path closes the hole in both at once. It covers
`~/.ssh`, `~/.codex`, `~/.aws`, `~/.gnupg`, `~/.docker`, `~/.config`, `~/.claude`,
`~/.kube`, `/root`, the whole data directory, and `settings.codex_config` wherever
`AGENT_HUB_CODEX_CONFIG` points it.

## The two backends

| | `sandboxed` | `unconfined` |
|---|---|---|
| Filesystem | host read-only, workspace writable | full access as the service user |
| Credentials | `~/.codex`, `~/.ssh`, `~/.aws`, `~/.config` masked with tmpfs | readable |
| Privilege | no sudo (uid map denies it) | passwordless sudo is available |
| Network | shared unless `AGENT_HUB_TOOL_NETWORK=0` | shared; the setting only stops `fetch` |
| Namespaces | pid, uts, ipc unshared; `--die-with-parent` | none |

`unconfined` is a real choice with a real consequence: an agent can read the
provider key out of `~/.codex/config.toml`. That is documented at the point of
choosing it in the UI, not hidden.

**There is no silent downgrade.** If a job asks for `sandboxed` and the backend is
unavailable, `build_sandbox()` raises and the job fails with the probe's reason.
Quietly running unconfined because the sandbox was missing would be the worst
available failure mode, so it is not a code path that exists.

## Current state of this host (2026-08-25)

Out of the box, `sandboxed` was **unavailable** here:

```
bwrap: setting up uid map: Permission denied
  — the kernel denies unprivileged user namespaces on this host
```

`bwrap` 0.9.0 is installed at `/usr/bin/bwrap` but is not setuid, and this host has
`kernel.apparmor_restrict_unprivileged_userns=1` (the Ubuntu 24.04 default), which
denies unprivileged user namespaces to any binary without an AppArmor profile
granting `userns`. **Provisioning option 1 below has since been applied**, and

```bash
bwrap --ro-bind / / -- /bin/echo ok        # prints: ok
```

now succeeds, and `check_sandbox` **exits 0** — so confinement here is a verified
claim rather than an intention. What it proved, by execution:

```
ok  [unconfined] no service configuration or secret in the environment — 10 variables, all expected
ok  [sandboxed]  no service configuration or secret in the environment — 10 variables, all expected
ok  cannot read the provider key (~/.codex/config.toml) — 0 bytes back, no key material
ok  cannot read the database (agent-hub.db) — 0 bytes back, no SQLite header
ok  cannot see anything else in the data directory — 4 on the host, none of them visible
ok  cannot escalate with sudo — exit 1
ok  can reach the network — https://example.com returned 200
ok  can write the workspace — proof.txt is on the host with the right contents
ok  cannot write outside the workspace — exit 2, /etc is read-only
ok  cannot see another job's workspace — exit 1, the sibling is not there
```

Re-run it after any change to the mask list. Three bugs sat between the first
working `bwrap` invocation and that clean run, and all three are reasons to:

- **A mask target that does not exist fails the whole command.** bwrap creates a
  missing mount point, and every masked path is under the read-only bind of `/`,
  so `--tmpfs ~/.aws` on a host with no `~/.aws` aborts with `Can't mkdir …:
  Read-only file system` — every sandboxed command, not just that mount.
  `wrap()` therefore filters the list by existence on each call.
- **Naming the database files was not enough.** `data/agent-hub.db.bak-2026-08-24`
  is a full copy of the job database under a name the per-file list did not
  enumerate, and a `-wal` appearing after the list is built would have been missed
  the same way. The whole data directory is masked now, and `check_sandbox`
  asserts on the *directory listing* so the next stray sibling is caught too.
- **The per-file masks then punched holes back through the directory mask.** With
  `--tmpfs data/` in place, a later `--ro-bind-try /dev/null data/agent-hub.db`
  makes bwrap *create* that mount point inside the tmpfs — so the file reappears in
  `ls`, empty. The three names the per-file list enumerated were exactly the three
  that came back; the `.bak` copy, named nowhere, stayed correctly hidden. Nothing
  was readable either way, which is why only an assertion on the *listing* found
  it. `_masked_paths()` now drops any file entry that falls inside a masked
  directory — absent beats present-but-empty — while keeping it in the list, so a
  path that later moves *out* of a masked directory is still covered.

Two things stay true regardless:

- **`unconfined` still exposes the provider key**, by design — an agent can `cat`
  `~/.codex/config.toml`. Treat the key as exposed to any job you run unconfined,
  and rotate it if such a job goes somewhere unexpected. `sandboxed` is the default.
- `tests/conftest.py` pins `AGENT_HUB_SANDBOX=unconfined` so the suite does not
  turn a host kernel setting into dozens of unrelated failures. Coverage of the
  bwrap path in the suite is thin by construction — the suite asserts the mask list
  is coherent, `check_sandbox` asserts the mounts actually confine.

## The guardrail is advisory; the sandbox is the boundary

Worth stating with an example, because the two get conflated. A live job asked to
`apt install ripgrep` tripped the `sudo` guardrail three times, and the operator
**approved** every one. All three still failed:

```text
sudo: /etc/sudo.conf is owned by uid 65534, should be 0
sudo: The "no new privileges" flag is set, which prevents sudo from running as root.
```

Approving a gate is permission to *attempt* a command, not permission to leave the
sandbox — and nothing in the approval path can grant the latter. The agent then
reported that it could not verify the install rather than claiming success, which is
the behaviour the role instructions are for.

The inverse is just as important: the guardrail is a regex over the command string,
so `getent group sudo` gates too. An agent probing its own privileges will stop for
a human two or three times before it gives up. That is the intended cost of putting
a human in the loop on a word rather than on an outcome — the regex cannot know
which `sudo` is the dangerous one, so it stops for all of them.

## Three live checks, because three bugs were only visible live

Each of these exists because the unit suite passed while the running service was
wrong. They need the service up, and `check_sandbox` needs the bwrap profile
installed.

```bash
.venv/bin/python -m tools.check_sandbox        # the mounts actually confine
.venv/bin/python -m tools.check_tool_restart   # a command interrupted by a restart
.venv/bin/python -m tools.check_stop_at_gate   # a stop while a gate is pending
```

The third is the newest and the least obvious. Stopping a job parked on a command
gate used to leave it reading `running` with no task behind it: the gate's own
status restore ran during cancellation and landed *after* the stop endpoint had
written `stopped`. Because the phases were already `skipped` and the startup sweep
deliberately skips terminal jobs, nothing ever corrected it — the job could not be
stopped, resumed or restarted out of that state. The rule the fix encodes is that
**a non-terminal status write must never overwrite a terminal one**, enforced in the
`UPDATE` itself rather than by a check before it, since every such check is one
`await` stale by the time the write lands.

## Provisioning option 1: an AppArmor profile for bwrap (recommended, applied here)

Narrowest change that fixes it — grants `userns` to bwrap alone, leaving the
host-wide restriction in place for everything else. The profile is version
controlled as `apparmor-bwrap.conf`, so install it from there rather than pasting a
heredoc:

```bash
sudo cp /home/ubuntu/agent-hub/apparmor-bwrap.conf /etc/apparmor.d/bwrap
sudo apparmor_parser -r /etc/apparmor.d/bwrap
```

Then confirm, in this order — each step proves something the next one assumes:

```bash
bwrap --ro-bind / / -- /bin/echo ok        # the kernel now permits it
sudo systemctl restart agent-hub           # the probe is cached at startup
curl -s 127.0.0.1:8090/api/sandbox         # the service agrees
cd /home/ubuntu/agent-hub && .venv/bin/python -m tools.check_sandbox   # expect 0
```

`check_sandbox` is the step that matters. It asserts against the real backend that
a confined command cannot read `~/.codex/config.toml`, the database, or any other
file in the data directory, cannot sudo, cannot write outside its workspace, cannot
see a sibling job's workspace, *can* still reach the network, and *can* write its
own workspace — and it fails loudly rather than passing if any of those checks
would be vacuous (no key file, no database, no sudo installed). Its three exits are
distinct on purpose:

| Exit | Meaning |
|---|---|
| 0 | every claim held, by execution |
| 1 | a claim did not hold, or would have been vacuous — the line says which |
| 2 | the backend is unavailable, so **nothing was proved** — not the same as proved false |

Until it exits 0, the word "sandboxed" in the UI is an intention rather than a
verified claim.

## Provisioning option 2: relax the restriction host-wide

```bash
echo 'kernel.apparmor_restrict_unprivileged_userns=0' \
  | sudo tee /etc/sysctl.d/60-userns.conf
sudo sysctl --system
```

Blunter: it re-enables unprivileged user namespaces for every process on the box,
not just bwrap. Option 1 is preferred for that reason. Both survive reboot.

## Provisioning option 3: a low-privilege runner user

Sidesteps user namespaces entirely — run commands as a `agent-hub-runner` user with
no sudo rights and no read access to the service's home, reached through a narrow
sudoers rule. This needs **a new backend in `app/orchestrator/sandbox.py`** and is
not implemented; it is the right answer on hosts where neither option above is
acceptable, and it would also give a cleaner story than tmpfs masking, since the
credentials would be unreadable by file permissions rather than by mount trickery.

Container-per-job was considered and rejected for now: shipping a picker option
that needs an image nobody has built is worse than not offering it.
