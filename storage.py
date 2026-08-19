import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
ACCOUNTS_FILE = os.path.join(DATA_DIR, 'accounts.json')


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_accounts():
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def save_account_meta(account_id, alias, region_count, resource_count):
    _ensure_dir()
    accounts = load_accounts()
    now = datetime.utcnow().isoformat() + 'Z'
    for acct in accounts:
        if acct['account_id'] == account_id:
            if alias:
                acct['alias'] = alias
            acct['last_scan'] = now
            acct['region_count'] = region_count
            acct['resource_count'] = resource_count
            break
    else:
        accounts.append({
            'account_id': account_id,
            'alias': alias or account_id,
            'last_scan': now,
            'region_count': region_count,
            'resource_count': resource_count,
        })
    with open(ACCOUNTS_FILE, 'w') as f:
        json.dump(accounts, f, indent=2)


def save_results(account_id, results):
    _ensure_dir()
    path = os.path.join(DATA_DIR, f'{account_id}.json')
    with open(path, 'w') as f:
        json.dump(results, f)


def load_results(account_id):
    path = os.path.join(DATA_DIR, f'{account_id}.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def compute_diff(old_results, new_results):
    """Compare two scan snapshots and return a change summary."""
    by_type = {}
    total_added = total_removed = total_unchanged = 0

    all_types = set()
    for k, v in old_results.items():
        if not k.startswith('_') and isinstance(v, list):
            all_types.add(k)
    for k, v in new_results.items():
        if not k.startswith('_') and isinstance(v, list):
            all_types.add(k)

    for rtype in sorted(all_types):
        old_ids = {r.get('id', '') for r in old_results.get(rtype, []) if r.get('id')}
        new_ids = {r.get('id', '') for r in new_results.get(rtype, []) if r.get('id')}

        added = len(new_ids - old_ids)
        removed = len(old_ids - new_ids)
        unchanged = len(old_ids & new_ids)

        total_added += added
        total_removed += removed
        total_unchanged += unchanged

        if added or removed:
            by_type[rtype] = {
                'added': added,
                'removed': removed,
                'unchanged': unchanged,
            }

    return {
        'total_added': total_added,
        'total_removed': total_removed,
        'total_unchanged': total_unchanged,
        'by_type': by_type,
    }


def delete_account(account_id):
    accounts = [a for a in load_accounts() if a['account_id'] != account_id]
    _ensure_dir()
    with open(ACCOUNTS_FILE, 'w') as f:
        json.dump(accounts, f, indent=2)
    path = os.path.join(DATA_DIR, f'{account_id}.json')
    if os.path.exists(path):
        os.remove(path)
