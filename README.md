# Dell warranty to Freshservice assets

Pulls Dell TechDirect entitlement data and writes it onto matching Freshservice assets, so a technician can see what a machine is actually covered for before they quote a repair.

Freshservice tracks *when* a warranty ends. It does not track *what* is covered. Those are different questions, and the gap costs money: a cracked screen or a liquid spill is only covered under Accidental Damage Protection, never under standard ProSupport or Basic. Without ADP visible in the asset record, the usual outcome is a technician quoting a paid repair on a machine that was covered, or promising a free one on a machine that was not.

This fills up to six fields on each Dell asset. A field is written only if your
helpdesk has one with a matching label, so the last two are optional:

| Field | Example | |
|---|---|---|
| Warranty Tier | `ProSupport Plus` | |
| Accidental Damage Protection | `Yes` | |
| Dell Coverage Detail | `Accidental Damage Service; ProSupport Plus; Keep Your Hard Drive` | |
| Warranty Expiry Date | `2028-04-17` | |
| Acquisition Date | `2025-04-17` | from Dell's ship date |
| Warranty | `36` | months, ship date to expiry |

## What it does not do

- It never touches non-Dell assets. Selection is by asset type and the Dell service tag format, which is Dell specific. Product-name matching is available too but off by default; see `require_product_match` below.
- It never creates or deletes assets. It only updates the fields above.
- It never writes `Unknown`. If Dell has no data, or the coverage is unrecognised, the existing value is left alone rather than overwritten with a guess.
- It never writes a value that is already correct, so a no-change run makes zero API calls to update anything.

## How it works

```
Freshservice assets  ->  filter to Dell  ->  Dell TechDirect  ->  Freshservice
   (asset type)          (service tag        (entitlements       (mapped fields,
                          format)             by service tag)     only if changed)
```

There is no database and no cache. Freshservice is the source of truth for which assets exist, and Dell is the source of truth for what they are covered for. Nothing is stored between runs.

## Setup

### 1. Create the Freshservice fields

Add these as custom fields on your **Hardware** asset type, so every child type (Laptop, Desktop, Monitor, Dock) inherits them:

| Label | Type | Choices |
|---|---|---|
| Warranty Tier | Dropdown | `ProSupport Plus`, `ProSupport`, `Premium Support`, `Basic Onsite (NBD)`, `Advanced Exchange`, `Return to Depot`, `Expired` |
| Accidental Damage Protection | Dropdown | `Yes`, `No`, `Unknown` |
| Dell Coverage Detail | Text | |

`Warranty Expiry Date`, `Acquisition Date` and `Warranty` (months) already exist
in Freshservice as built-in fields, and are filled from Dell's ship date and
entitlement end date when present.

Freshservice computes `Warranty Expiry Date` itself, but only when
`Acquisition Date` or `Warranty` length is filled in or changed. The tool works
with that rather than against it, so you do not have to choose between the three
fields. See [Why the expiry date is written in a second
call](#why-the-expiry-date-is-written-in-a-second-call).

A dry run warns if a resolved field cannot hold the value, for example if your
`Warranty` field is a date rather than a number.

The dropdown choices must match the left-hand values in your config's `tiers` list exactly. Freshservice rejects a value that is not one of the defined choices.

**You do not need to look up the field keys.** Freshservice names them like `warranty_tier_21001234567`, where the number is your tenant's asset type id. This tool resolves keys by **label** at runtime, so the same code works in any helpdesk without edits.

### 2. Get Dell TechDirect API access

Dell's warranty API is part of TechDirect. Request API access there and you receive an OAuth client id and secret. Access is granted per organisation and is not instant.

### 3. Configure

```bash
cp config.example.json config.json
```

Edit `config.json`. Every option is documented inline. The one you are most likely to change is `asset_types`.

Dell entitles monitors, docks and servers as well as computers, so all of them are
scanned by default:

```json
["Laptop", "Desktop", "Server", "Windows Server", "Monitor", "Dock", "Tablet"]
```

**Child types are not included by their parent.** A PowerEdge is often recorded
under `Windows Server` or `VMware Server` rather than `Server`, and naming the
parent does not pull in the children. List every child type you actually use.
`Dock` is normally a type you added yourself, so check its exact name.

Virtual machines cost nothing to leave in the list. Their asset tags are not Dell
service tags, so the tag filter drops them before any Dell call.

`field_labels` is merged slot by slot with the built-in defaults, so naming one
slot leaves the rest alone. Set a slot to `[]` to switch that field off
deliberately.

`skip_asset_states` defaults to `["Retired", "Disposed"]`. A machine that has
left service needs no warranty data, and syncing it spends a write and fills the
log. The names are matched without regard to case against the asset's **Asset
State** field, so put your own names there if you renamed them. Set it to `[]` to
sync every state.

`require_product_match` is `false` by default. Assets are selected on the Dell
service tag format alone, because most Freshservice records are named after the
hostname rather than the model, and a name-based filter would skip them. Set it
to `true` only if your records reliably carry the model. Either way the
service-tag collision guard below still runs.

### 4. Provide credentials

Four environment variables, never the config file and never the command line:

```bash
export DELL_CLIENT_ID=...
export DELL_CLIENT_SECRET=...
export FRESHSERVICE_DOMAIN=yourcompany.freshservice.com   # no https://
export FRESHSERVICE_API_KEY=...
```

The Freshservice key must belong to an agent with permission to edit assets, and that agent must be a member of the workspace holding them. A key with the right role but the wrong workspace returns `403` on every call.

**Use a service account, not a person.** A key tied to an individual stops working the day they leave, and the only symptom is warranty data quietly going stale.

## Running it

```bash
python3 dell_fs_sync.py --selftest    # offline checks, no credentials needed
python3 dell_fs_sync.py               # dry run: prints what would change
python3 dell_fs_sync.py --apply       # writes
```

Useful flags:

| Flag | Purpose |
|---|---|
| `--only TAG` | Sync only this service tag. Repeatable. Use it for the first live write, on a machine you can check by eye. |
| `--limit N` | Process at most N assets. Good for a first look. |
| `--max-writes N` | Stop after N writes. Defaults to 500 from config. `0` removes the cap. |
| `--config PATH` | Use a different config file. |

Dry run is the default on purpose. You have to ask for writes.

A dry run also checks your setup before it can cost you anything:

- Every field label in your config is resolved against the real Freshservice
  fields. A label that matches nothing prints a `WARN`. If no label at all
  matches, the run stops rather than reporting a clean no-op.
- Every value the tool could write into a dropdown is compared with that
  dropdown's defined choices. A missing choice prints a `WARN` naming the field
  and the values. This is the failure you are most likely to hit on a first
  deploy, and it is better read here than mid-write.

Labels are matched without regard to case, so `Warranty Tier` and
`warranty tier` both work in your config.

A write Freshservice rejects is reported, counted, and the run carries on to the
next asset. One bad value cannot abandon the rest of the estate.

Exit codes, for whatever schedules it: `0` success, `1` an API or network
failure or one or more rejected writes, `2` a bad config, `130` interrupted. A
failure prints one line to stderr rather than a traceback.

Python 3.8 or newer. **No third-party packages**, standard library only.

## Deploying it

This repo is the source, not a scheduler. It carries no opinion about where it
runs, and none of the options below is the blessed one. Pick the one that fits
what you already have.

### What any target needs

- **Python 3.8 or newer.** No third-party packages, so no virtualenv, no
  `pip install`, no lock file. One file.
- **Four environment variables**: `DELL_CLIENT_ID`, `DELL_CLIENT_SECRET`,
  `FRESHSERVICE_DOMAIN`, `FRESHSERVICE_API_KEY`. Never on the command line, and
  never in `config.json`.
- **`config.json` is optional.** Without one the built-in defaults apply. See
  [Configure](#3-configure).
- **Outbound HTTPS** to `apigtwb2c.us.dell.com` and to your Freshservice tenant.
  Nothing inbound, no open port, no database.
- **A schedule.** Weekly is plenty; warranty coverage moves slowly.
- **Something that notices a failure.** See below.

The [exit codes](#running-it) are the contract between the script and whatever
schedules it. Nothing else needs to parse the output.

### You must set up failure alerting, wherever you run it

This matters more than the choice of host. A sync that stops silently leaves
stale coverage on the asset, and **stale coverage data reads exactly like current
coverage data**. Nobody opens a ticket about a warranty tier that looks filled in.

So whichever target you pick, make a non-zero exit reach a mailbox a person
actually reads. That is an alert rule, a job-failure notification, or a wrapper
that mails the output. The script gives you the exit code; it cannot make anyone
listen.

### Two things that actually differ between targets

Everything else is much the same, so compare on these:

1. **Where the Freshservice key lives.** That key can write to your asset
   database. Some teams need it inside their own tenant; for others a secret store
   in the CI system is fine. This is a policy question for whoever owns the
   helpdesk, not a technical one.
2. **Whether the schedule can be switched off without you.** A managed scheduler
   you own keeps running. A CI schedule may be disabled for you after a period of
   repository inactivity, and this repo will go quiet once the sync works. Check
   your provider's current rule rather than trusting this README on the detail.

### Azure Automation

One file with no third-party imports loads as a Python 3 runbook with no
packaging work.

1. In your Automation Account, create a **Python 3** runtime environment if you
   do not have one.
2. Create a runbook, paste in `dell_fs_sync.py`, and publish it.
3. Add the four credentials as **encrypted Automation variables**, or keep them in
   Key Vault and read them with the Automation Account's managed identity.
4. At the top of the runbook, read them into the environment:

   ```python
   import automationassets, os
   for name in ("DELL_CLIENT_ID", "DELL_CLIENT_SECRET",
                "FRESHSERVICE_DOMAIN", "FRESHSERVICE_API_KEY"):
       os.environ[name] = automationassets.get_automation_variable(name)
   ```

5. Attach a schedule, and an alert on the failed-job metric.

Tradeoff: the runbook is a copy, so it drifts from `main`. Deploy it from the repo,
or record the deployed commit in the runbook description so you can tell what is
actually running.

### GitHub Actions

[.github/workflows/sync.yml](.github/workflows/sync.yml) is included and runs on
demand only. Add the four credentials as repository secrets and trigger it from
the Actions tab. It dry-runs unless you set `apply` to `true`.

To schedule it, add a `schedule:` trigger, in a private fork you control rather
than a public clone.

Tradeoffs: the credentials live with your CI provider rather than in your own
tenant; a scheduled workflow can be disabled after repository inactivity; and
hosted runners egress from changing public addresses, so check first if either API
is restricted by source IP.

### cron, Task Scheduler, or a container

It is an ordinary Python script, so an existing server or scheduled task works
with no new platform at all. Export the four variables, run the script, and send a
non-zero exit somewhere.

Tradeoff: you own the host, the Python version, and the log retention. If you
already run scheduled jobs somewhere and have a place logs go, this is the least
new machinery of the three.

## What Dell returns, per product type

The tier is decided by matching Dell's `serviceLevelDescription` against the
`tiers` patterns, first match wins. These are real responses, one per product
type, and they are pinned in `--selftest` so a change to the patterns cannot
break them quietly:

| Product | Dell coverage description | Tier written |
|---|---|---|
| Laptop | `Complete Care / Accidental Damage` + `Return To Depot Support` | `Return to Depot`, and ADP `Yes` |
| Server | `Onsite Service After Remote Diagnosis (Consumer Customer)/ Next Business Day Onsite After Remote Diagnosis (for business Customer)` | `Basic Onsite (NBD)` |
| Monitor | `Advanced Exchange Support` | `Advanced Exchange` |
| Dock | `Advanced Exchange Support` | `Advanced Exchange` |

Two things worth knowing:

- **Monitors and docks come back as `Advanced Exchange Support`.** If that tier is
  missing from your dropdown, every monitor and dock write fails. A dry run warns
  you before that happens.
- **`Complete Care` is Dell's accidental damage product**, and it is not always
  spelled out next to the words "Accidental Damage". Both wordings count as ADP,
  because a false negative here tells a technician a cracked screen is not
  covered.

An unrecognised but still active coverage stays `Unknown`, and `Unknown` is never
written. So a new Dell product name leaves the old value in place rather than
overwriting it with something wrong. If you see monitors syncing with a blank
tier, that is the symptom.

## Checking that a product type really is syncing

The run prints a breakdown, so you do not have to assume:

```
candidate Dell assets: 412  (36 had no service tag)
  serials that did not look like a service tag, for example: Windows Server=ASSET-1234
  by asset type: Desktop 44, Dock 96, Laptop 168, Monitor 101, Server 3
```

If a product type is missing from `by asset type`, work through it in this order:

1. Is its asset type name in `asset_types`, including the child type?
2. Is it counted under "had no service tag"? Then the serial in Freshservice is
   not a 7-character Dell service tag. Fix the records, or widen
   `service_tag_pattern`.
3. Is it counted under "skipped by asset state"? Then it is Retired or Disposed.
4. Does it appear under "no Dell data"? Then Dell does not recognise the tag.

## Why the expiry date is written in a second call

Freshservice computes `Warranty Expiry Date` from `Acquisition Date` plus
`Warranty` months, whenever either of those two is filled in or changed.

That collides with what we want. Dell gives an exact entitlement end date. The
date Freshservice computes is an acquisition date plus a whole number of months,
so it can be about two weeks out. In one test it was 11 days early. A technician
reading a date 11 days early quotes a paid repair on a machine that is still
covered, which is the whole reason this tool exists.

Sent in one `PUT`, the computed date lands on top of Dell's date and wins. So the
tool sends two calls instead:

1. Everything else, including `Acquisition Date` and `Warranty` months.
2. `Warranty Expiry Date` on its own.

The second call changes neither trigger field, so Freshservice does not
recompute, and Dell's exact date stays. The summary counts these:

```
split writes : 37  (the expiry date goes in a second call, so Freshservice cannot compute over it)
```

Only assets that need both halves cost two calls. An asset whose acquisition date
and warranty length are already correct takes one call, or none.

The expiry date is re-sent even when Freshservice already holds the right value,
because the first call would otherwise recompute it away and the field would flap
between runs. A steady-state re-run still writes nothing at all.

If a run fails between the two calls, the asset holds the computed date rather
than Dell's, and the log says so:

```
NOTE LT-JSMITH-01 was partly written; re-run to finish it
```

Re-running fixes it. If you would rather let Freshservice own the expiry date,
set `"warranty_expiry": []` in `field_labels` and the tool leaves it alone.

## Design notes

A few decisions that are less obvious than they look.

**Field keys are resolved by label, not hardcoded.** Freshservice keys embed the tenant's asset type id. Hardcoding one makes the tool work in exactly one helpdesk forever.

**`Expired` means every entitlement has ended, not "coverage I did not recognise".** An earlier version marked anything whose active coverage was unrecognised as `Expired`. That put the word `Expired` beside a future warranty end date, and a technician quoted a paid repair on a covered machine. Unrecognised but still active coverage is reported as `Unknown` and is not written. When Dell introduces a plan name this tool has never seen, the failure mode is a blank field, not a wrong one.

**Tier matching is first-match-wins on an ordered list.** `ProSupport Plus` must be tested before `ProSupport`, or every Plus machine is recorded as plain ProSupport.

**Dates compare on the date part only.** Freshservice returns dates with a time component and Dell does not. A naive string comparison rewrites every asset on every run.

**There is a write cap.** A bad config or an upstream change should not be able to rewrite an entire estate before anyone notices.

**Retries are limited to 429 and 5xx.** A `403` or a `404` will not fix itself on a retry, and retrying auth failures just multiplies them.

**Config is merged, not replaced, one level deep.** A config that named a single `field_labels` slot used to delete every other slot, which silently switched off two of the six fields for anyone who copied the example file. Naming a slot now overrides only that slot.

**The collision guard reads the name and the model fields, and deliberately not `description`.** An asset is usually named after its hostname, which names no product family at all, so a guard reading the name alone almost never fired. But `description` is free text where technicians leave notes, and a note only *mentions* a model, it does not claim the asset is one. All three of these blocked a laptop from ever syncing again:

```
"will not charge through the WD19 dock"    -> read as dock wd19
"replaced under case S1234 by the vendor"  -> read as monitor s1234
"swapped the P2725 for a bigger one"       -> read as monitor p2725
```

The last two only became possible when monitor model codes were added to
`product_family`, which is that function's own warning playing out: a pattern
that fails to recognise a product is safe, and a pattern that recognises the
wrong thing is not. Widening what counts as a model widens what a note can be
mistaken for.

Blocking is not a harmless default. A blocked asset is never written again, so
its coverage goes quietly stale, and stale coverage reads exactly like current
coverage. So the guard reads only fields that state what the machine is, while
the looser text still feeds `product_match`, where matching too eagerly only
lets an asset through to the service-tag check.

## Tests

`--selftest` runs 105 offline checks covering tier matching, ADP detection, the
expired rule, date comparison, config merging and validation, label
normalisation, field resolution by label, the dropdown-choice and field-type
pre-flights, asset-state reading, the split write that protects the expiry date,
the service-tag collision guard, and the no-write-when-unchanged and
never-write-Unknown guards. No network, no credentials.

The tier, ADP and collision-guard checks use coverage descriptions and product
names taken from real Dell and Freshservice responses for a laptop, a server, a
monitor and a dock, rather than invented ones.

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs them on every push and
pull request, on Python 3.8 and 3.12, and checks that `config.example.json` is
still a config the tool accepts. The sync workflow runs them again before every
sync.

## Licence

[The Unlicense](LICENSE). Public domain. Use it, change it, fork it, ship it,
sell it. You owe nothing, not even attribution.
