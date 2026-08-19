import io
import logging
import threading
from datetime import datetime

import boto3
from flask import Flask, jsonify, render_template, request, send_file

import exporter
import scanner
import storage

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
app = Flask(__name__)


@app.route('/')
def index():
    return render_template('index.html')


# ── Accounts ────────────────────────────────────────────────────────────────

@app.route('/api/accounts')
def list_accounts():
    return jsonify(storage.load_accounts())


@app.route('/api/accounts/<account_id>/load', methods=['POST'])
def load_account(account_id):
    results = storage.load_results(account_id)
    if not results:
        return jsonify({'error': 'No saved results for this account'}), 404
    scanner.set_results(results)
    return jsonify({'message': 'Loaded', 'account_id': account_id})


@app.route('/api/accounts/<account_id>', methods=['DELETE'])
def delete_account(account_id):
    storage.delete_account(account_id)
    return jsonify({'message': 'Deleted'})


# ── Scan ────────────────────────────────────────────────────────────────────

@app.route('/api/scan', methods=['POST'])
def start_scan():
    status = scanner.get_status()
    if status['running']:
        return jsonify({'error': 'Scan already in progress'}), 409

    data = request.get_json() or {}
    access_key = (data.get('access_key') or '').strip()
    secret_key = (data.get('secret_key') or '').strip()
    session_token = (data.get('session_token') or '').strip() or None
    alias = (data.get('alias') or '').strip()

    if not access_key or not secret_key:
        return jsonify({'error': 'Access Key ID and Secret Access Key are required.'}), 400

    try:
        session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )
        identity = session.client('sts').get_caller_identity()
        account_id = identity['Account']
    except Exception as e:
        return jsonify({'error': f'Authentication failed: {e}'}), 401

    credentials = {
        'access_key': access_key,
        'secret_key': secret_key,
        'session_token': session_token,
    }

    # Snapshot previous results before scanning so we can diff afterward
    old_results = storage.load_results(account_id)

    def do_scan():
        scanner.run_scan(credentials)
        results = scanner.get_results()
        acct = results.get('_account_id', '')
        if not acct:
            return

        total = sum(
            len(v) for k, v in results.items()
            if not k.startswith('_') and isinstance(v, list)
        )
        regions = len(results.get('_regions', []))
        ts = datetime.utcnow().isoformat() + 'Z'

        # Diff against prior scan
        changes = None
        if old_results:
            changes = storage.compute_diff(old_results, results)

        results['_changes'] = changes
        results['_scan_timestamp'] = ts

        # Full replacement — overwrites all previous data on disk
        storage.save_results(acct, results)
        storage.save_account_meta(acct, alias, regions, total)

        # Push changes + timestamp into in-memory results
        scanner.update_results(_changes=changes, _scan_timestamp=ts)

        # Update status bar with diff summary for re-scans
        if changes and (changes['total_added'] or changes['total_removed']):
            scanner._update_status(
                message=(
                    f'Re-scan complete — {total:,} resources. '
                    f'+{changes["total_added"]} added, '
                    f'-{changes["total_removed"]} removed, '
                    f'{changes["total_unchanged"]} unchanged.'
                ),
            )

    threading.Thread(target=do_scan, daemon=True).start()
    return jsonify({'message': 'Scan started', 'account_id': account_id})


# ── Region scan ─────────────────────────────────────────────────────────────

@app.route('/api/scan/region/<path:region_name>', methods=['POST'])
def start_region_scan(region_name):
    status = scanner.get_status()
    if status['running']:
        return jsonify({'error': 'Scan already in progress'}), 409

    results = scanner.get_results()
    if not results.get('_account_id'):
        return jsonify({'error': 'No account loaded — run a full scan first.'}), 400

    data = request.get_json() or {}
    access_key = (data.get('access_key') or '').strip()
    secret_key = (data.get('secret_key') or '').strip()
    session_token = (data.get('session_token') or '').strip() or None

    if not access_key or not secret_key:
        return jsonify({'error': 'Access Key ID and Secret Access Key are required.'}), 400

    try:
        session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )
        session.client('sts').get_caller_identity()
    except Exception as e:
        return jsonify({'error': f'Authentication failed: {e}'}), 401

    credentials = {
        'access_key': access_key,
        'secret_key': secret_key,
        'session_token': session_token,
    }

    def do_region_scan():
        scanner.run_region_scan(credentials, region_name)
        results = scanner.get_results()
        acct = results.get('_account_id', '')
        if acct:
            total = sum(
                len(v) for k, v in results.items()
                if not k.startswith('_') and isinstance(v, list)
            )
            regions = len(results.get('_regions', []))
            storage.save_results(acct, results)
            storage.save_account_meta(acct, None, regions, total)

    threading.Thread(target=do_region_scan, daemon=True).start()
    return jsonify({'message': f'Region scan started for {region_name}'})


# ── Resources ───────────────────────────────────────────────────────────────

@app.route('/api/status')
def get_status():
    return jsonify(scanner.get_status())


@app.route('/api/resources')
def get_resources():
    results = scanner.get_results()
    summary = {}
    for key, items in results.items():
        if key.startswith('_') or not isinstance(items, list):
            continue
        summary[key] = {
            'count': len(items),
            'label': scanner.RESOURCE_LABELS.get(key, key),
        }
    summary['_regions'] = results.get('_regions', [])
    summary['_account_id'] = results.get('_account_id', '')
    summary['_changes'] = results.get('_changes')
    summary['_scan_timestamp'] = results.get('_scan_timestamp', '')
    return jsonify(summary)


@app.route('/api/resources/<resource_type>')
def get_resource_type(resource_type):
    results = scanner.get_results()
    resources = results.get(resource_type, [])
    return jsonify({
        'type': resource_type,
        'label': scanner.RESOURCE_LABELS.get(resource_type, resource_type),
        'count': len(resources),
        'resources': resources,
    })


# ── VPC view ───────────────────────────────────────────────────────────────

@app.route('/api/vpcs')
def list_vpcs():
    results = scanner.get_results()
    vpcs = results.get('vpcs', [])
    out = []
    for vpc in vpcs:
        vid = vpc['id']
        counts = {}
        total = 0
        for key, items in results.items():
            if key.startswith('_') or not isinstance(items, list) or key == 'vpcs':
                continue
            n = sum(1 for r in items if r.get('vpc_id') == vid)
            if n:
                counts[key] = n
                total += n
        out.append({**vpc, 'resource_counts': counts, 'total_resources': total})
    out.sort(key=lambda v: (-v['total_resources'], v.get('region', ''), v.get('name', '')))
    return jsonify(out)


@app.route('/api/vpcs/<path:vpc_id>')
def get_vpc_detail(vpc_id):
    results = scanner.get_results()
    vpc_info = next((v for v in results.get('vpcs', []) if v['id'] == vpc_id), None)
    if not vpc_info:
        return jsonify({'error': 'VPC not found'}), 404

    groups = {}
    total = 0
    for key, items in results.items():
        if key.startswith('_') or not isinstance(items, list) or key == 'vpcs':
            continue
        matched = [r for r in items if r.get('vpc_id') == vpc_id]
        if matched:
            groups[key] = {
                'label': scanner.RESOURCE_LABELS.get(key, key),
                'count': len(matched),
                'resources': matched,
            }
            total += len(matched)

    return jsonify({'vpc': vpc_info, 'groups': groups, 'total_resources': total})


# ── Maps ───────────────────────────────────────────────────────────────────

@app.route('/api/maps/peering')
def peering_map():
    results = scanner.get_results()
    peerings = results.get('vpc_peering', [])
    local_vpcs = {v['id']: v for v in results.get('vpcs', [])}
    account_id = results.get('_account_id', '')

    nodes = {}
    edges = []
    seen = set()

    for p in peerings:
        pid = p.get('id', '')
        if pid in seen:
            continue
        seen.add(pid)

        req_id = p.get('requester_vpc', '')
        acc_id = p.get('accepter_vpc', '')

        for vpc_id, prefix in [(req_id, 'requester'), (acc_id, 'accepter')]:
            if not vpc_id or vpc_id in nodes:
                continue
            if vpc_id in local_vpcs:
                v = local_vpcs[vpc_id]
                nodes[vpc_id] = {
                    'id': vpc_id,
                    'name': v.get('name', ''),
                    'region': v.get('region', ''),
                    'cidr': v.get('cidr', ''),
                    'account': account_id,
                    'external': False,
                }
            else:
                nodes[vpc_id] = {
                    'id': vpc_id,
                    'name': '',
                    'region': p.get(f'{prefix}_region', ''),
                    'cidr': p.get(f'{prefix}_cidr', ''),
                    'account': p.get(f'{prefix}_owner', ''),
                    'external': p.get(f'{prefix}_owner', '') != account_id,
                }

        if req_id and acc_id:
            edges.append({
                'id': pid,
                'name': p.get('name', ''),
                'source': req_id,
                'target': acc_id,
                'state': p.get('state', ''),
            })

    return jsonify({
        'type': 'peering',
        'title': 'VPC Peering Connections',
        'nodes': list(nodes.values()),
        'edges': edges,
    })


# ── Region view ────────────────────────────────────────────────────────────

@app.route('/api/regions')
def list_regions():
    results = scanner.get_results()
    regions = results.get('_regions', [])
    out = []
    for region in sorted(regions):
        counts = {}
        total = 0
        for key, items in results.items():
            if key.startswith('_') or not isinstance(items, list):
                continue
            n = sum(1 for r in items if r.get('region') == region)
            if n:
                counts[key] = n
                total += n
        if total:
            out.append({'region': region, 'resource_counts': counts, 'total_resources': total})
    # Also include 'global' if any resources have region='global'
    global_counts = {}
    global_total = 0
    for key, items in results.items():
        if key.startswith('_') or not isinstance(items, list):
            continue
        n = sum(1 for r in items if r.get('region') == 'global')
        if n:
            global_counts[key] = n
            global_total += n
    if global_total:
        out.append({'region': 'global', 'resource_counts': global_counts, 'total_resources': global_total})
    out.sort(key=lambda r: (-r['total_resources'], r['region']))
    return jsonify(out)


@app.route('/api/regions/<path:region_name>')
def get_region_detail(region_name):
    results = scanner.get_results()
    groups = {}
    total = 0
    for key, items in results.items():
        if key.startswith('_') or not isinstance(items, list):
            continue
        matched = [r for r in items if r.get('region') == region_name]
        if matched:
            groups[key] = {
                'label': scanner.RESOURCE_LABELS.get(key, key),
                'count': len(matched),
                'resources': matched,
            }
            total += len(matched)
    return jsonify({'region': region_name, 'groups': groups, 'total_resources': total})


# ── Export ──────────────────────────────────────────────────────────────────

@app.route('/api/export')
def export_excel():
    results = scanner.get_results()
    if not any(isinstance(v, list) for k, v in results.items() if not k.startswith('_')):
        return jsonify({'error': 'No scan results — run a scan first'}), 400

    try:
        data = exporter.generate_excel(results)
    except Exception as e:
        logging.exception('Export failed')
        return jsonify({'error': str(e)}), 500

    account_id = results.get('_account_id', 'unknown')
    timestamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
    filename = f'aws-inventory-{account_id}-{timestamp}.xlsx'

    return send_file(
        io.BytesIO(data),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename,
    )


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='AWS Resource Inventory')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()
    print(f'\n  AWS Inventory running at http://{args.host}:{args.port}\n')
    app.run(host=args.host, port=args.port, debug=args.debug)
