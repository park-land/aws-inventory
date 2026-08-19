# AWS Resource Inventory

A self-hosted web tool that scans an AWS account across every enabled
region, inventories what's in it (EC2, VPCs, RDS, Lambda, S3, IAM, and more),
lets you browse the results by resource type, by VPC, or by region, diffs
each scan against the previous one, and exports everything to a formatted
Excel workbook.

## Running it

Build the image and run it with a volume for persisted scan data:

```bash
docker build -t aws-inventory .
docker run -p 5000:5000 -d --name aws-inventory -v ~/aws-inventory-data:/app/data aws-inventory
```

Then open **http://localhost:5000**.

- The `-v ~/aws-inventory-data:/app/data` mount is what makes scan results
  survive a container restart — each scanned account gets a JSON snapshot
  on the host at `~/aws-inventory-data/<account_id>.json`, plus an
  `accounts.json` index. Drop the volume mount if you'd rather run it
  throwaway, with nothing kept between runs.
- To stop/remove it: `docker stop aws-inventory && docker rm aws-inventory`.
- To update after pulling changes: `docker build -t aws-inventory .` again,
  then `docker stop`/`rm`/`run` (the volume keeps your data through that).

### Running without Docker

```bash
pip install -r requirements.txt
python app.py --host 127.0.0.1 --port 5000
```

## Using it

1. Open the app and click **Scan Account**.
2. Paste in an AWS access key, secret key, and (if using temporary/STS
   credentials) session token. Optionally give the account an alias so it's
   recognizable in the account dropdown later.
3. The scan runs across every enabled region plus the global services (S3,
   IAM, CloudFront, Route53). Progress shows live.
4. Browse results by resource type, by VPC, or by region using the tabs.
   VPC peering connections also have a map view. Switch accounts anytime
   from the dropdown in the top bar — previously scanned accounts are
   loaded from disk, not re-scanned.
5. Re-scanning an account you've scanned before shows a changes banner
   (added / removed / unchanged) against the prior snapshot.
6. **Export** downloads the current account's inventory as an `.xlsx`
   workbook, grouped by VPC/region with one section per resource type.

## On credentials

This app **never persists AWS credentials to disk and never reads them from
the environment, `~/.aws/credentials`, or an instance profile.** Every scan
requires pasting credentials into the web UI, and they're held in memory
only for the duration of that scan's background thread — never logged,
never written to `data/`, never returned in any API response. Only scan
*results* are saved between runs, keyed by AWS account ID.

This is a deliberate tradeoff, not a missing feature. The tool is meant to
be run ad hoc against whichever account needs checking this time — often
short-lived STS credentials for a client engagement or a one-off audit —
and to keep a history of scans across *many* different accounts over time.
Wiring in environment-variable or profile-based auth would make it too easy
to scan the wrong account by accident, and persisting long-lived keys on
disk is a liability this tool has no reason to take on. If you need
long-running, unattended, single-account monitoring instead, this isn't the
right tool — look at AWS Config / Trusted Advisor / a dedicated CSPM
product instead.

## Required AWS permissions

The credentials used to scan need read-only (`Describe*`/`List*`/`Get*`)
access to the services being inventoried — EC2 (including VPC, subnets,
security groups, ELB/ALB/NLB), RDS, Lambda, ECS/EKS, ElastiCache, DynamoDB,
SNS/SQS, Auto Scaling, S3, CloudFront, Route53, ACM, OpenSearch, Elastic
Beanstalk, and IAM (users/roles/policies). `ReadOnlyAccess` (AWS managed
policy) or an equivalent scoped-down read-only policy is sufficient — the
app never calls a mutating API.

## Data layout

```
data/
  accounts.json        # list of scanned accounts: id, alias, last scan time, counts
  <account_id>.json    # full resource snapshot for that account (overwritten each scan)
```

Delete an account's data from the UI (trash icon next to the account
dropdown) or just remove its file from the `data/` directory directly.
