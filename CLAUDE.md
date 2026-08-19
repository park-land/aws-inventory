# CLAUDE.md

Guidance for Claude Code (and future contributors) working in this repository.

## What this is

A small Flask app that scans one or more AWS accounts across all enabled
regions, inventories resources by type/VPC/region, diffs each scan against
the previous one for that account, and exports everything to Excel. There is
no database — scan results are cached in memory while the server runs and
persisted as JSON files under `data/` (see `storage.py`), one file per AWS
account ID.

## Architecture

- **`app.py`** — Flask routes. Thin: parses requests, kicks off scans on a
  background thread, and shapes `scanner`'s in-memory results into the JSON
  the frontend expects (grouped by VPC, by region, by resource type, etc).
- **`scanner.py`** — All AWS API calls. Holds the scanner registry
  (`REGIONAL_SCANNERS` / `GLOBAL_SCANNERS`), the in-memory scan state
  (`_scan_status`, `_scan_results`), and the threaded scan runner
  (`run_scan`, `run_region_scan`).
- **`storage.py`** — Reads/writes `data/accounts.json` (account list +
  metadata) and `data/<account_id>.json` (one full snapshot per account).
  Also computes the added/removed/unchanged diff between two snapshots.
- **`exporter.py`** — Builds the multi-sheet Excel workbook from a results
  dict. Has its own copies of the column/field/label maps described below
  (see "Duplication across files").
- **`templates/index.html`** — The entire frontend: one HTML file with
  inline `<style>` and `<script>`, no build step, no frontend framework
  beyond Bootstrap/DataTables/D3 loaded from CDN-style `<link>`/`<script>`
  tags. Talks to the Flask JSON API with `fetch`.

## Running locally (without Docker)

```bash
pip install -r requirements.txt
python app.py --host 127.0.0.1 --port 5000
```

See `README.md` for the Docker workflow, which is the intended way to run
this day-to-day.

## Credentials: deliberately not persisted, not read from the environment

AWS credentials are entered by hand in the web UI for every scan (access
key, secret key, optional session token) and posted as JSON to `/api/scan`.
From there:

- They live only in a local Python variable for the life of that scan's
  background thread (`scanner.run_scan` / `run_region_scan`), used to build
  a `boto3.Session`.
- They are **never** written to `data/`, logged, or included in any response
  body. Only scan *results* are persisted, never the credentials used to
  produce them.
- The app never falls back to environment variables, `~/.aws/credentials`,
  an instance profile, or any other ambient credential source — `boto3` is
  always constructed with the exact keys the request supplied.

This is intentional, not an oversight: the tool is meant to be run ad hoc
against a rotating cast of accounts (client engagements, one-off audits,
"whoever's turn it is to check this quarter"), often with temporary STS
credentials that shouldn't outlive the browser tab. Baking in env-var or
profile-based auth would make it too easy to silently scan the wrong
account, and persisting long-lived keys to disk is a liability this app has
no reason to take on. If you're tempted to add an env-var credential
fallback or a "remember my credentials" checkbox, don't — ask first, since
this is a considered decision, not a gap.

## Adding a new resource type to the inventory

Resource types are wired through several places by convention, not by a
plugin system — there's no registry-of-registries. To add one (say,
`efs_file_systems`), touch all of the following:

1. **`scanner.py`** — write the scan function and register it.
   - Regional resource (scanned per-region): signature `(session, region)`.
     Global resource (IAM, S3, Route53, CloudFront, etc.): signature
     `(session)` only.
   - Wrap the AWS calls in `try/except ClientError` and `logger.warning(...)`
     on failure — one region/account lacking a service or permission should
     never abort the whole scan. Return `[]` on error, not raise.
   - Every returned dict should include a `'name'` (use the `_name(tags)`
     helper if the resource has a tags list), a stable `'id'`, and — for
     regional resources — `'region'`. If the resource is VPC-scoped, include
     `'vpc_id'` too (used for the "By VPC" grouping).
   - Use paginators (`session.client(...).get_paginator(...)`) wherever the
     underlying API supports one; several existing scanners are good
     templates (`scan_ec2_instances`, `scan_vpcs`, `scan_lambda_functions`).
   - Add a tuple `('efs_file_systems', 'EFS File Systems', scan_efs_file_systems)`
     to `REGIONAL_SCANNERS` or `GLOBAL_SCANNERS` near the bottom of the file.
     `RESOURCE_LABELS` is derived from these lists automatically — nothing
     else to update there.

2. **`exporter.py`** — add matching entries, keyed by the same resource-type
   string, to all three of:
   - `RESOURCE_COLUMNS` (human-readable Excel column headers, in order)
   - `RESOURCE_FIELDS` (the dict keys to pull from each resource, same order
     as `RESOURCE_COLUMNS`)
   - `RESOURCE_LABELS` (section title used in the workbook)
   And add the key to either `VPC_RESOURCE_TYPES` or `GLOBAL_RESOURCE_TYPES`
   depending on whether it's VPC-scoped, so it lands in the right workbook
   sheet.

3. **`templates/index.html`** — add matching entries, keyed the same way, to:
   - `LABELS` — display name used across the UI.
   - `COLUMNS` — table column headers for the detail view.
   - `FIELDS` — dict keys pulled from each resource, same order as `COLUMNS`.
   - `CATEGORIES` — add the type string to the `types` array of whichever
     category it belongs to (Compute, Networking, Storage, Database,
     Messaging, Security & Identity, CDN & DNS), or add a new category
     object if it doesn't fit an existing one.

   Yes, `COLUMNS`/`FIELDS`/`LABELS` are near-duplicates of
   `RESOURCE_COLUMNS`/`RESOURCE_FIELDS`/`RESOURCE_LABELS` in `exporter.py`.
   This is existing, intentional-enough duplication (frontend vs. Excel
   export evolved separately) — keep both in sync rather than trying to
   unify them as a drive-by change.

4. **Optional — VPC/region grouping metadata.** If the new resource needs
   special handling beyond the generic `vpc_id`/`region` grouping (e.g. it's
   summarized differently on the VPC or region cards), check
   `app.py`'s `/api/vpcs`, `/api/vpcs/<vpc_id>`, `/api/regions`, and
   `/api/regions/<region_name>` handlers — they iterate all resource types
   generically via `resource_counts`, so most new types need no changes
   there at all.

5. **Optional — a map view.** Only `vpc_peering` currently has a graph/map
   visualization (`MAP_REGISTRY` in `templates/index.html`, rendered via
   `/api/maps/peering` in `app.py` + D3 in the frontend). Only add a map for
   a new resource type if it represents a *relationship between resources*
   (peering, transit gateway attachments, etc.) — not for a plain resource
   list.

After wiring a new type, run a scan against an account that actually has
that resource and check: the resource-type card count, the detail table
(columns line up with data), the VPC/Region groupings if applicable, and
the Excel export sheet.

## Conventions

- No build step, no bundler, no test suite currently exists — this is a
  small internal tool. Keep changes consistent with that: single-file
  frontend, synchronous-looking but thread-backed scans, JSON files for
  storage.
- Frontend JS in `index.html` is written dense/minified-by-hand (no
  whitespace, short function bodies) — match that style rather than
  reformatting it wholesale when editing.
- `scanner.py` functions never raise on a per-resource-type failure; keep
  that pattern for any new scanner so one broken permission doesn't kill an
  entire account scan.
