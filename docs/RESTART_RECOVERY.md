# Windows paper-worker recovery

The October 29 repair keeps the existing books, account IDs, strategy settings,
exits, costs, schedules and deadlines. It does not initialize accounts, enable
paid AI, sign transactions or change paper execution.

Research exports use four bounded file-writing threads (64 queued addresses per
batch). Database reads and qualification calculations stay on the main thread.
Every file retains its existing content, observation/qualification timestamps,
fsync and atomic replacement. The index publishes only after all address files
succeed. A failed write cannot publish a successful index or completion report.
`runtime/research/publication-status.json` records the current run and phase so a
timeout is distinguishable from a completed export. The 240-second child limit,
provider budgets, 1,000/day history cap and three-batch rotation are unchanged.

New Jev/research tasks use Normal priority. Existing Jev is migrated explicitly;
its cadence, 50-second child limit, model gate, assessment deduplication and
ambiguous-call no-retry rule remain unchanged. Desktop worker status includes PID,
Windows session, phase and completed duration; a started process is not success.

## Authorized boot recovery

`scripts/enable-restart-recovery.ps1` needs one-time Windows administrator consent
to register boot/noninteractive tasks. It changes only the eleven named production
tasks belonging to the specified installation. It preserves their actions,
interpreters, recurring/daily triggers, limits and overlap rules. It uses the SAME
Windows user with limited privileges and S4U, not SYSTEM or stored passwords.
No automatic Windows login, update suppression or credential duplication is used.

Microsoft documents that S4U lacks reusable network credentials and encrypted-file
access: [Task logon types](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-logontype-principaltype-element).
Do not assume it can run another installation. Before ANY production conversion,
the script runs `restart_check.py` in an isolated Session 0 task. That probe must
read the actual paper account identities and unencrypted credential files,
confirm paid calls remain disabled, and complete public HTTPS plus both existing
RPC `eth_chainId` reads. Only the tested public/bearer HTTPS access is established;
Windows-authenticated shares, EFS, DPAPI and live signers are not covered.

The script saves before/after task XML and protected configuration hashes. It
fails closed if ownership, accounts, permissions or network checks differ, and
rolls back task definitions if migration fails. A 30-second boot trigger is added
to workers and the loopback console. The daily report receives NO boot trigger,
so restarting cannot consume the 21:30 report. Existing recurrence still provides
retries after delayed network availability. Existing OS locks, seen-once and
ledger duplicate controls continue to decide admission.

Run elevated, pointing to the existing installation and a NEW evidence folder:

```powershell
& .\scripts\enable-restart-recovery.ps1 -AppRoot 'C:\Users\User\Downloads\sniper-v0.1.0' -EvidenceDirectory 'C:\Dev\SNIPER\sniper-publish\runtime\recovery\migration-unique'
```

Verify actual completed journal/export runs and task result/session evidence,
not just task registration. A Session 0 task run tests the noninteractive execution
context; it is not a claim that a physical reboot was performed. Do not reboot or
sign the user out merely to manufacture that evidence. Preserve all missed
collection blocks and monitoring gaps in the seven-day evaluation.
