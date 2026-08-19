import boto3
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from botocore.exceptions import ClientError
from datetime import datetime

logger = logging.getLogger(__name__)

_scan_status = {
    'running': False,
    'completed': False,
    'progress': 0,
    'total': 0,
    'message': 'Ready — click Scan Account to begin.',
    'error': None,
    'started_at': None,
    'completed_at': None,
}
_scan_results = {}
_status_lock = threading.Lock()


def _fmt_dt(dt):
    if dt is None:
        return ''
    return dt.isoformat() if hasattr(dt, 'isoformat') else str(dt)


def _name(tags):
    if not tags:
        return ''
    for tag in tags:
        if tag.get('Key') == 'Name':
            return tag.get('Value', '')
    return ''


# ── Region discovery ─────────────────────────────────────────────────────────

def get_enabled_regions(session):
    try:
        ec2 = session.client('ec2', region_name='us-east-1')
        resp = ec2.describe_regions(
            Filters=[{'Name': 'opt-in-status', 'Values': ['opt-in-not-required', 'opted-in']}]
        )
        return sorted(r['RegionName'] for r in resp['Regions'])
    except Exception as e:
        logger.error('Failed to get regions: %s', e)
        return ['us-east-1', 'us-west-2', 'eu-west-1', 'ap-southeast-1']


# ── Regional scanners (session, region) ──────────────────────────────────────

def scan_ec2_instances(session, region):
    out = []
    try:
        ec2 = session.client('ec2', region_name=region)
        for page in ec2.get_paginator('describe_instances').paginate():
            for res in page['Reservations']:
                for i in res['Instances']:
                    out.append({
                        'name': _name(i.get('Tags')),
                        'id': i['InstanceId'],
                        'type': i.get('InstanceType', ''),
                        'state': i['State']['Name'],
                        'private_ip': i.get('PrivateIpAddress', ''),
                        'public_ip': i.get('PublicIpAddress', ''),
                        'key_name': i.get('KeyName', ''),
                        'launch_time': _fmt_dt(i.get('LaunchTime')),
                        'image_id': i.get('ImageId', ''),
                        'platform': i.get('Platform', 'linux'),
                        'vpc_id': i.get('VpcId', ''),
                        'subnet_id': i.get('SubnetId', ''),
                        'region': region,
                    })
    except ClientError as e:
        logger.warning('EC2 instances %s: %s', region, e)
    return out


def scan_vpcs(session, region):
    out = []
    try:
        ec2 = session.client('ec2', region_name=region)
        for page in ec2.get_paginator('describe_vpcs').paginate():
            for v in page['Vpcs']:
                out.append({
                    'name': _name(v.get('Tags')),
                    'id': v['VpcId'],
                    'cidr': v['CidrBlock'],
                    'state': v['State'],
                    'is_default': v['IsDefault'],
                    'owner_id': v.get('OwnerId', ''),
                    'vpc_id': v['VpcId'],
                    'region': region,
                })
    except ClientError as e:
        logger.warning('VPCs %s: %s', region, e)
    return out


def scan_subnets(session, region):
    out = []
    try:
        ec2 = session.client('ec2', region_name=region)
        for page in ec2.get_paginator('describe_subnets').paginate():
            for s in page['Subnets']:
                out.append({
                    'name': _name(s.get('Tags')),
                    'id': s['SubnetId'],
                    'cidr': s['CidrBlock'],
                    'az': s['AvailabilityZone'],
                    'available_ips': s['AvailableIpAddressCount'],
                    'state': s['State'],
                    'map_public_ip': s.get('MapPublicIpOnLaunch', False),
                    'vpc_id': s['VpcId'],
                    'region': region,
                })
    except ClientError as e:
        logger.warning('Subnets %s: %s', region, e)
    return out


def scan_security_groups(session, region):
    out = []
    try:
        ec2 = session.client('ec2', region_name=region)
        for page in ec2.get_paginator('describe_security_groups').paginate():
            for sg in page['SecurityGroups']:
                out.append({
                    'name': sg.get('GroupName', ''),
                    'id': sg['GroupId'],
                    'description': sg.get('Description', ''),
                    'inbound_rules': len(sg.get('IpPermissions', [])),
                    'outbound_rules': len(sg.get('IpPermissionsEgress', [])),
                    'vpc_id': sg.get('VpcId', ''),
                    'region': region,
                })
    except ClientError as e:
        logger.warning('Security groups %s: %s', region, e)
    return out


def scan_route_tables(session, region):
    out = []
    try:
        ec2 = session.client('ec2', region_name=region)
        for page in ec2.get_paginator('describe_route_tables').paginate():
            for rt in page['RouteTables']:
                main = any(a.get('Main') for a in rt.get('Associations', []))
                out.append({
                    'name': _name(rt.get('Tags')),
                    'id': rt['RouteTableId'],
                    'main': main,
                    'routes': len(rt.get('Routes', [])),
                    'associations': len(rt.get('Associations', [])),
                    'vpc_id': rt['VpcId'],
                    'region': region,
                })
    except ClientError as e:
        logger.warning('Route tables %s: %s', region, e)
    return out


def scan_internet_gateways(session, region):
    out = []
    try:
        ec2 = session.client('ec2', region_name=region)
        for page in ec2.get_paginator('describe_internet_gateways').paginate():
            for igw in page['InternetGateways']:
                attachments = igw.get('Attachments', [])
                out.append({
                    'name': _name(igw.get('Tags')),
                    'id': igw['InternetGatewayId'],
                    'vpc_id': attachments[0]['VpcId'] if attachments else '',
                    'state': attachments[0]['State'] if attachments else 'detached',
                    'region': region,
                })
    except ClientError as e:
        logger.warning('IGWs %s: %s', region, e)
    return out


def scan_nat_gateways(session, region):
    out = []
    try:
        ec2 = session.client('ec2', region_name=region)
        for page in ec2.get_paginator('describe_nat_gateways').paginate():
            for ngw in page['NatGateways']:
                out.append({
                    'name': _name(ngw.get('Tags')),
                    'id': ngw['NatGatewayId'],
                    'type': ngw.get('ConnectivityType', 'public'),
                    'state': ngw['State'],
                    'subnet_id': ngw.get('SubnetId', ''),
                    'vpc_id': ngw.get('VpcId', ''),
                    'region': region,
                })
    except ClientError as e:
        logger.warning('NAT gateways %s: %s', region, e)
    return out


def scan_elastic_ips(session, region):
    out = []
    try:
        ec2 = session.client('ec2', region_name=region)
        for addr in ec2.describe_addresses()['Addresses']:
            out.append({
                'name': _name(addr.get('Tags')),
                'id': addr.get('AllocationId', addr.get('PublicIp', '')),
                'public_ip': addr.get('PublicIp', ''),
                'private_ip': addr.get('PrivateIpAddress', ''),
                'instance_id': addr.get('InstanceId', ''),
                'association_id': addr.get('AssociationId', ''),
                'domain': addr.get('Domain', ''),
                'vpc_id': '',
                'region': region,
            })
    except ClientError as e:
        logger.warning('Elastic IPs %s: %s', region, e)
    return out


def scan_volumes(session, region):
    out = []
    try:
        ec2 = session.client('ec2', region_name=region)
        for page in ec2.get_paginator('describe_volumes').paginate():
            for vol in page['Volumes']:
                attachments = vol.get('Attachments', [])
                out.append({
                    'name': _name(vol.get('Tags')),
                    'id': vol['VolumeId'],
                    'type': vol.get('VolumeType', ''),
                    'size': vol.get('Size', 0),
                    'state': vol['State'],
                    'az': vol['AvailabilityZone'],
                    'encrypted': vol.get('Encrypted', False),
                    'instance_id': attachments[0]['InstanceId'] if attachments else '',
                    'vpc_id': '',
                    'region': region,
                })
    except ClientError as e:
        logger.warning('Volumes %s: %s', region, e)
    return out


def scan_load_balancers_classic(session, region):
    out = []
    try:
        elb = session.client('elb', region_name=region)
        for page in elb.get_paginator('describe_load_balancers').paginate():
            for lb in page['LoadBalancerDescriptions']:
                out.append({
                    'name': lb['LoadBalancerName'],
                    'id': lb['LoadBalancerName'],
                    'dns_name': lb.get('DNSName', ''),
                    'scheme': lb.get('Scheme', ''),
                    'instances': len(lb.get('Instances', [])),
                    'type': 'classic',
                    'vpc_id': lb.get('VPCId', ''),
                    'region': region,
                })
    except ClientError as e:
        if 'AccessDenied' not in str(e) and 'UnauthorizedOperation' not in str(e):
            logger.warning('Classic ELB %s: %s', region, e)
    return out


def scan_load_balancers_v2(session, region):
    out = []
    try:
        elbv2 = session.client('elbv2', region_name=region)
        for page in elbv2.get_paginator('describe_load_balancers').paginate():
            for lb in page['LoadBalancers']:
                out.append({
                    'name': lb['LoadBalancerName'],
                    'id': lb['LoadBalancerArn'],
                    'dns_name': lb.get('DNSName', ''),
                    'type': lb.get('Type', ''),
                    'scheme': lb.get('Scheme', ''),
                    'state': lb['State']['Code'],
                    'created': _fmt_dt(lb.get('CreatedTime')),
                    'vpc_id': lb.get('VpcId', ''),
                    'region': region,
                })
    except ClientError as e:
        logger.warning('ELBv2 %s: %s', region, e)
    return out


def scan_rds_instances(session, region):
    out = []
    try:
        rds = session.client('rds', region_name=region)
        for page in rds.get_paginator('describe_db_instances').paginate():
            for db in page['DBInstances']:
                vpc_id = ''
                if db.get('DBSubnetGroup'):
                    vpc_id = db['DBSubnetGroup'].get('VpcId', '')
                out.append({
                    'name': db['DBInstanceIdentifier'],
                    'id': db['DBInstanceIdentifier'],
                    'engine': f"{db.get('Engine', '')} {db.get('EngineVersion', '')}".strip(),
                    'instance_class': db.get('DBInstanceClass', ''),
                    'state': db.get('DBInstanceStatus', ''),
                    'multi_az': db.get('MultiAZ', False),
                    'storage': db.get('AllocatedStorage', 0),
                    'endpoint': db.get('Endpoint', {}).get('Address', '') if db.get('Endpoint') else '',
                    'vpc_id': vpc_id,
                    'region': region,
                })
    except ClientError as e:
        logger.warning('RDS instances %s: %s', region, e)
    return out


def scan_rds_clusters(session, region):
    out = []
    try:
        rds = session.client('rds', region_name=region)
        for page in rds.get_paginator('describe_db_clusters').paginate():
            for c in page['DBClusters']:
                out.append({
                    'name': c['DBClusterIdentifier'],
                    'id': c['DBClusterIdentifier'],
                    'engine': f"{c.get('Engine', '')} {c.get('EngineVersion', '')}".strip(),
                    'state': c.get('Status', ''),
                    'multi_az': c.get('MultiAZ', False),
                    'members': len(c.get('DBClusterMembers', [])),
                    'endpoint': c.get('Endpoint', ''),
                    'vpc_id': '',
                    'region': region,
                })
    except ClientError as e:
        logger.warning('RDS clusters %s: %s', region, e)
    return out


def scan_lambda_functions(session, region):
    out = []
    try:
        lmb = session.client('lambda', region_name=region)
        for page in lmb.get_paginator('list_functions').paginate():
            for fn in page['Functions']:
                vpc_cfg = fn.get('VpcConfig') or {}
                out.append({
                    'name': fn['FunctionName'],
                    'id': fn['FunctionArn'],
                    'runtime': fn.get('Runtime', ''),
                    'handler': fn.get('Handler', ''),
                    'state': fn.get('State', 'Active'),
                    'memory': fn.get('MemorySize', 0),
                    'timeout': fn.get('Timeout', 0),
                    'code_size': fn.get('CodeSize', 0),
                    'last_modified': fn.get('LastModified', ''),
                    'vpc_id': vpc_cfg.get('VpcId', ''),
                    'region': region,
                })
    except ClientError as e:
        logger.warning('Lambda %s: %s', region, e)
    return out


def scan_ecs_clusters(session, region):
    out = []
    try:
        ecs = session.client('ecs', region_name=region)
        arns = []
        for page in ecs.get_paginator('list_clusters').paginate():
            arns.extend(page['clusterArns'])
        for i in range(0, len(arns), 100):
            for c in ecs.describe_clusters(clusters=arns[i:i+100], include=['STATISTICS'])['clusters']:
                out.append({
                    'name': c['clusterName'],
                    'id': c['clusterArn'],
                    'status': c['status'],
                    'running_tasks': c.get('runningTasksCount', 0),
                    'pending_tasks': c.get('pendingTasksCount', 0),
                    'active_services': c.get('activeServicesCount', 0),
                    'vpc_id': '',
                    'region': region,
                })
    except ClientError as e:
        logger.warning('ECS %s: %s', region, e)
    return out


def scan_eks_clusters(session, region):
    out = []
    try:
        eks = session.client('eks', region_name=region)
        for page in eks.get_paginator('list_clusters').paginate():
            for name in page['clusters']:
                try:
                    c = eks.describe_cluster(name=name)['cluster']
                    out.append({
                        'name': c['name'],
                        'id': c['arn'],
                        'version': c.get('version', ''),
                        'status': c['status'],
                        'endpoint': c.get('endpoint', ''),
                        'created': _fmt_dt(c.get('createdAt')),
                        'vpc_id': (c.get('resourcesVpcConfig') or {}).get('vpcId', ''),
                        'region': region,
                    })
                except ClientError as e:
                    logger.warning('EKS describe %s %s: %s', name, region, e)
    except ClientError as e:
        logger.warning('EKS %s: %s', region, e)
    return out


def scan_elasticache_clusters(session, region):
    out = []
    try:
        ec = session.client('elasticache', region_name=region)
        for page in ec.get_paginator('describe_cache_clusters').paginate():
            for c in page['CacheClusters']:
                vpc_id = ''
                if c.get('CacheSubnetGroupName'):
                    try:
                        sg = ec.describe_cache_subnet_groups(
                            CacheSubnetGroupName=c['CacheSubnetGroupName']
                        )['CacheSubnetGroups']
                        if sg:
                            vpc_id = sg[0].get('VpcId', '')
                    except ClientError:
                        pass
                out.append({
                    'name': c['CacheClusterId'],
                    'id': c['CacheClusterId'],
                    'engine': f"{c.get('Engine', '')} {c.get('EngineVersion', '')}".strip(),
                    'node_type': c.get('CacheNodeType', ''),
                    'status': c.get('CacheClusterStatus', ''),
                    'nodes': c.get('NumCacheNodes', 0),
                    'vpc_id': vpc_id,
                    'region': region,
                })
    except ClientError as e:
        logger.warning('ElastiCache %s: %s', region, e)
    return out


def scan_dynamodb_tables(session, region):
    out = []
    try:
        ddb = session.client('dynamodb', region_name=region)
        for page in ddb.get_paginator('list_tables').paginate():
            for name in page['TableNames']:
                try:
                    t = ddb.describe_table(TableName=name)['Table']
                    out.append({
                        'name': t['TableName'],
                        'id': t.get('TableArn', t['TableName']),
                        'status': t.get('TableStatus', ''),
                        'items': t.get('ItemCount', 0),
                        'size_bytes': t.get('TableSizeBytes', 0),
                        'billing_mode': (t.get('BillingModeSummary') or {}).get('BillingMode', 'PROVISIONED'),
                        'vpc_id': '',
                        'region': region,
                    })
                except ClientError as e:
                    logger.warning('DynamoDB describe %s %s: %s', name, region, e)
    except ClientError as e:
        logger.warning('DynamoDB %s: %s', region, e)
    return out


def scan_sns_topics(session, region):
    out = []
    try:
        sns = session.client('sns', region_name=region)
        for page in sns.get_paginator('list_topics').paginate():
            for topic in page['Topics']:
                arn = topic['TopicArn']
                out.append({
                    'name': arn.split(':')[-1],
                    'id': arn,
                    'vpc_id': '',
                    'region': region,
                })
    except ClientError as e:
        logger.warning('SNS %s: %s', region, e)
    return out


def scan_sqs_queues(session, region):
    out = []
    try:
        sqs = session.client('sqs', region_name=region)
        for page in sqs.get_paginator('list_queues').paginate():
            for url in page.get('QueueUrls', []):
                out.append({
                    'name': url.split('/')[-1],
                    'id': url,
                    'url': url,
                    'vpc_id': '',
                    'region': region,
                })
    except ClientError as e:
        logger.warning('SQS %s: %s', region, e)
    return out


def scan_autoscaling_groups(session, region):
    out = []
    try:
        asg = session.client('autoscaling', region_name=region)
        for page in asg.get_paginator('describe_auto_scaling_groups').paginate():
            for g in page['AutoScalingGroups']:
                out.append({
                    'name': g['AutoScalingGroupName'],
                    'id': g['AutoScalingGroupARN'],
                    'min_size': g['MinSize'],
                    'max_size': g['MaxSize'],
                    'desired': g['DesiredCapacity'],
                    'instances': len(g.get('Instances', [])),
                    'vpc_zones': g.get('VPCZoneIdentifier', ''),
                    'vpc_id': '',
                    'region': region,
                })
    except ClientError as e:
        logger.warning('ASG %s: %s', region, e)
    return out


def scan_vpc_endpoints(session, region):
    out = []
    try:
        ec2 = session.client('ec2', region_name=region)
        for page in ec2.get_paginator('describe_vpc_endpoints').paginate():
            for ep in page['VpcEndpoints']:
                out.append({
                    'name': _name(ep.get('Tags')),
                    'id': ep['VpcEndpointId'],
                    'service': ep.get('ServiceName', ''),
                    'type': ep.get('VpcEndpointType', ''),
                    'state': ep.get('State', ''),
                    'vpc_id': ep.get('VpcId', ''),
                    'region': region,
                })
    except ClientError as e:
        logger.warning('VPC endpoints %s: %s', region, e)
    return out


def scan_vpc_peering_connections(session, region):
    out = []
    try:
        ec2 = session.client('ec2', region_name=region)
        for page in ec2.get_paginator('describe_vpc_peering_connections').paginate():
            for pc in page['VpcPeeringConnections']:
                req = pc.get('RequesterVpcInfo', {})
                acc = pc.get('AccepterVpcInfo', {})
                out.append({
                    'name': _name(pc.get('Tags')),
                    'id': pc['VpcPeeringConnectionId'],
                    'state': (pc.get('Status') or {}).get('Code', ''),
                    'requester_vpc': req.get('VpcId', ''),
                    'requester_cidr': req.get('CidrBlock', ''),
                    'requester_owner': req.get('OwnerId', ''),
                    'requester_region': req.get('Region', ''),
                    'accepter_vpc': acc.get('VpcId', ''),
                    'accepter_cidr': acc.get('CidrBlock', ''),
                    'accepter_owner': acc.get('OwnerId', ''),
                    'accepter_region': acc.get('Region', ''),
                    'vpc_id': req.get('VpcId', ''),
                    'region': region,
                })
    except ClientError as e:
        logger.warning('VPC peering %s: %s', region, e)
    return out


def scan_transit_gateways(session, region):
    out = []
    try:
        ec2 = session.client('ec2', region_name=region)
        for page in ec2.get_paginator('describe_transit_gateways').paginate():
            for tgw in page['TransitGateways']:
                out.append({
                    'name': _name(tgw.get('Tags')),
                    'id': tgw['TransitGatewayId'],
                    'state': tgw.get('State', ''),
                    'description': tgw.get('Description', ''),
                    'owner_id': tgw.get('OwnerId', ''),
                    'vpc_id': '',
                    'region': region,
                })
    except ClientError as e:
        logger.warning('Transit gateways %s: %s', region, e)
    return out


def scan_snapshots(session, region):
    out = []
    try:
        ec2 = session.client('ec2', region_name=region)
        for page in ec2.get_paginator('describe_snapshots').paginate(OwnerIds=['self']):
            for snap in page['Snapshots']:
                out.append({
                    'name': _name(snap.get('Tags')),
                    'id': snap['SnapshotId'],
                    'volume_id': snap.get('VolumeId', ''),
                    'state': snap.get('State', ''),
                    'volume_size': snap.get('VolumeSize', 0),
                    'encrypted': snap.get('Encrypted', False),
                    'start_time': _fmt_dt(snap.get('StartTime')),
                    'description': (snap.get('Description') or '')[:120],
                    'vpc_id': '',
                    'region': region,
                })
    except ClientError as e:
        logger.warning('Snapshots %s: %s', region, e)
    return out


def scan_opensearch_domains(session, region):
    out = []
    try:
        client = session.client('opensearch', region_name=region)
        names = [d['DomainName'] for d in client.list_domain_names().get('DomainNames', [])]
        for i in range(0, len(names), 5):
            for d in client.describe_domains(DomainNames=names[i:i+5]).get('DomainStatusList', []):
                cfg = d.get('ClusterConfig') or {}
                vpc_opts = d.get('VPCOptions') or {}
                out.append({
                    'name': d['DomainName'],
                    'id': d.get('ARN', d['DomainName']),
                    'engine_version': d.get('EngineVersion', ''),
                    'instance_type': cfg.get('InstanceType', ''),
                    'instance_count': cfg.get('InstanceCount', 0),
                    'state': 'processing' if d.get('Processing') else 'active',
                    'endpoint': d.get('Endpoint') or (d.get('Endpoints') or {}).get('vpc', ''),
                    'vpc_id': vpc_opts.get('VPCId', ''),
                    'region': region,
                })
    except ClientError as e:
        logger.warning('OpenSearch %s: %s', region, e)
    return out


def scan_beanstalk_environments(session, region):
    out = []
    try:
        eb = session.client('elasticbeanstalk', region_name=region)
        resp = eb.describe_environments(IncludeDeleted=False)
        for env in resp.get('Environments', []):
            out.append({
                'name': env['EnvironmentName'],
                'id': env.get('EnvironmentId', ''),
                'application': env.get('ApplicationName', ''),
                'solution_stack': env.get('SolutionStackName', ''),
                'status': env.get('Status', ''),
                'health': env.get('Health', ''),
                'url': env.get('CNAME', ''),
                'tier': (env.get('Tier') or {}).get('Name', ''),
                'vpc_id': '',
                'region': region,
            })
    except ClientError as e:
        if 'OptInRequired' not in str(e):
            logger.warning('Beanstalk %s: %s', region, e)
    return out


def scan_acm_certificates(session, region):
    out = []
    try:
        acm = session.client('acm', region_name=region)
        for page in acm.get_paginator('list_certificates').paginate():
            for cert in page['CertificateSummaryList']:
                out.append({
                    'name': cert.get('DomainName', ''),
                    'id': cert['CertificateArn'],
                    'domain': cert.get('DomainName', ''),
                    'status': cert.get('Status', ''),
                    'type': cert.get('Type', ''),
                    'in_use': cert.get('InUse', False),
                    'key_algorithm': cert.get('KeyAlgorithm', ''),
                    'not_after': _fmt_dt(cert.get('NotAfter')),
                    'created': _fmt_dt(cert.get('CreatedAt')),
                    'vpc_id': '',
                    'region': region,
                })
    except ClientError as e:
        logger.warning('ACM %s: %s', region, e)
    return out


# ── Global scanners (session only, no region) ────────────────────────────────

def scan_s3_buckets(session):
    out = []
    try:
        s3 = session.client('s3', region_name='us-east-1')
        for bucket in s3.list_buckets().get('Buckets', []):
            name = bucket['Name']
            region = 'us-east-1'
            versioning = 'Disabled'
            try:
                loc = s3.get_bucket_location(Bucket=name)
                region = loc['LocationConstraint'] or 'us-east-1'
            except ClientError:
                pass
            try:
                ver = s3.get_bucket_versioning(Bucket=name)
                versioning = ver.get('Status', 'Disabled') or 'Disabled'
            except ClientError:
                pass
            out.append({
                'name': name, 'id': name,
                'created': _fmt_dt(bucket.get('CreationDate')),
                'versioning': versioning, 'vpc_id': '', 'region': region,
            })
    except ClientError as e:
        logger.warning('S3: %s', e)
    return out


def scan_cloudfront_distributions(session):
    out = []
    try:
        cf = session.client('cloudfront', region_name='us-east-1')
        for page in cf.get_paginator('list_distributions').paginate():
            for dist in (page.get('DistributionList') or {}).get('Items', []):
                origins = ', '.join(
                    o['DomainName'] for o in (dist.get('Origins') or {}).get('Items', [])
                )
                out.append({
                    'name': dist.get('Comment') or dist['Id'],
                    'id': dist['Id'],
                    'domain': dist.get('DomainName', ''),
                    'status': dist.get('Status', ''),
                    'enabled': dist.get('Enabled', False),
                    'price_class': dist.get('PriceClass', ''),
                    'origins': origins, 'vpc_id': '', 'region': 'global',
                })
    except ClientError as e:
        logger.warning('CloudFront: %s', e)
    return out


def scan_route53_zones(session):
    out = []
    try:
        r53 = session.client('route53', region_name='us-east-1')
        for page in r53.get_paginator('list_hosted_zones').paginate():
            for zone in page['HostedZones']:
                out.append({
                    'name': zone['Name'].rstrip('.'),
                    'id': zone['Id'].split('/')[-1],
                    'private': zone['Config'].get('PrivateZone', False),
                    'record_count': zone.get('ResourceRecordSetCount', 0),
                    'comment': zone['Config'].get('Comment', ''),
                    'vpc_id': '', 'region': 'global',
                })
    except ClientError as e:
        logger.warning('Route53: %s', e)
    return out


def scan_iam_users(session):
    out = []
    try:
        iam = session.client('iam')
        for page in iam.get_paginator('list_users').paginate():
            for u in page['Users']:
                out.append({
                    'name': u['UserName'],
                    'id': u['UserId'],
                    'arn': u['Arn'],
                    'path': u.get('Path', '/'),
                    'created': _fmt_dt(u.get('CreateDate')),
                    'password_last_used': _fmt_dt(u.get('PasswordLastUsed')),
                    'vpc_id': '',
                    'region': 'global',
                })
    except ClientError as e:
        logger.warning('IAM users: %s', e)
    return out


def scan_iam_roles(session):
    out = []
    try:
        iam = session.client('iam')
        for page in iam.get_paginator('list_roles').paginate():
            for r in page['Roles']:
                out.append({
                    'name': r['RoleName'],
                    'id': r['RoleId'],
                    'arn': r['Arn'],
                    'path': r.get('Path', '/'),
                    'created': _fmt_dt(r.get('CreateDate')),
                    'description': r.get('Description', ''),
                    'max_session': r.get('MaxSessionDuration', 3600),
                    'vpc_id': '',
                    'region': 'global',
                })
    except ClientError as e:
        logger.warning('IAM roles: %s', e)
    return out


def scan_iam_policies(session):
    out = []
    try:
        iam = session.client('iam')
        for page in iam.get_paginator('list_policies').paginate(Scope='Local'):
            for p in page['Policies']:
                out.append({
                    'name': p['PolicyName'],
                    'id': p['PolicyId'],
                    'arn': p['Arn'],
                    'path': p.get('Path', '/'),
                    'created': _fmt_dt(p.get('CreateDate')),
                    'updated': _fmt_dt(p.get('UpdateDate')),
                    'attachment_count': p.get('AttachmentCount', 0),
                    'vpc_id': '',
                    'region': 'global',
                })
    except ClientError as e:
        logger.warning('IAM policies: %s', e)
    return out


# ── Scanner registry ─────────────────────────────────────────────────────────

REGIONAL_SCANNERS = [
    ('ec2_instances',          'EC2 Instances',          scan_ec2_instances),
    ('vpcs',                   'VPCs',                   scan_vpcs),
    ('subnets',                'Subnets',                scan_subnets),
    ('security_groups',        'Security Groups',        scan_security_groups),
    ('route_tables',           'Route Tables',           scan_route_tables),
    ('internet_gateways',      'Internet Gateways',      scan_internet_gateways),
    ('nat_gateways',           'NAT Gateways',           scan_nat_gateways),
    ('elastic_ips',            'Elastic IPs',            scan_elastic_ips),
    ('volumes',                'EBS Volumes',            scan_volumes),
    ('load_balancers_classic', 'Classic Load Balancers', scan_load_balancers_classic),
    ('load_balancers_v2',      'ALB/NLB Load Balancers', scan_load_balancers_v2),
    ('rds_instances',          'RDS Instances',          scan_rds_instances),
    ('rds_clusters',           'RDS Clusters',           scan_rds_clusters),
    ('lambda_functions',       'Lambda Functions',       scan_lambda_functions),
    ('ecs_clusters',           'ECS Clusters',           scan_ecs_clusters),
    ('eks_clusters',           'EKS Clusters',           scan_eks_clusters),
    ('elasticache_clusters',   'ElastiCache Clusters',   scan_elasticache_clusters),
    ('dynamodb_tables',        'DynamoDB Tables',        scan_dynamodb_tables),
    ('sns_topics',             'SNS Topics',             scan_sns_topics),
    ('sqs_queues',             'SQS Queues',             scan_sqs_queues),
    ('autoscaling_groups',     'Auto Scaling Groups',    scan_autoscaling_groups),
    ('vpc_endpoints',          'VPC Endpoints',              scan_vpc_endpoints),
    ('vpc_peering',            'VPC Peering Connections',    scan_vpc_peering_connections),
    ('transit_gateways',       'Transit Gateways',           scan_transit_gateways),
    ('snapshots',              'EBS Snapshots',              scan_snapshots),
    ('opensearch_domains',     'OpenSearch Domains',         scan_opensearch_domains),
    ('beanstalk_environments', 'Beanstalk Environments',     scan_beanstalk_environments),
    ('acm_certificates',       'ACM Certificates',           scan_acm_certificates),
]

GLOBAL_SCANNERS = [
    ('s3_buckets',               'S3 Buckets',               scan_s3_buckets),
    ('cloudfront_distributions', 'CloudFront Distributions', scan_cloudfront_distributions),
    ('route53_zones',            'Route53 Hosted Zones',     scan_route53_zones),
    ('iam_users',                'IAM Users',                scan_iam_users),
    ('iam_roles',                'IAM Roles',                scan_iam_roles),
    ('iam_policies',             'IAM Policies',             scan_iam_policies),
]

RESOURCE_LABELS = {k: label for k, label, _ in REGIONAL_SCANNERS + GLOBAL_SCANNERS}


# ── State access ─────────────────────────────────────────────────────────────

def get_status():
    with _status_lock:
        return dict(_scan_status)


def get_results():
    with _status_lock:
        return dict(_scan_results)


def set_results(results):
    with _status_lock:
        _scan_results.clear()
        _scan_results.update(results)
        total = sum(len(v) for k, v in results.items() if not k.startswith('_') and isinstance(v, list))
        regions = len(results.get('_regions', []))
        _scan_status.update({
            'running': False,
            'completed': True,
            'message': f'Loaded saved results — {total:,} resources across {regions} regions.',
            'error': None,
        })


def update_results(**kwargs):
    """Merge extra metadata keys into in-memory results."""
    with _status_lock:
        _scan_results.update(kwargs)


def _update_status(**kwargs):
    with _status_lock:
        _scan_status.update(kwargs)


# ── Main scan entry point ───────────────────────────────────────────────────

def run_region_scan(credentials, region):
    """Scan a single region and merge results into existing data."""
    session = boto3.Session(
        aws_access_key_id=credentials['access_key'],
        aws_secret_access_key=credentials['secret_key'],
        aws_session_token=credentials.get('session_token'),
    )

    total_tasks = len(REGIONAL_SCANNERS) + len(GLOBAL_SCANNERS)
    _update_status(
        running=True, completed=False, progress=0, total=total_tasks,
        message=f'Scanning {region}…', error=None,
        started_at=datetime.utcnow().isoformat(), completed_at=None,
    )

    try:
        results_lock = threading.Lock()
        region_results = {k: [] for k, _, _ in REGIONAL_SCANNERS}
        global_results = {k: [] for k, _, _ in GLOBAL_SCANNERS}
        completed = [0]

        def scan_and_collect(key, scanner_fn, label):
            items = scanner_fn(session, region)
            with results_lock:
                region_results[key].extend(items)
                completed[0] += 1
                _update_status(
                    progress=completed[0],
                    message=f'Scanned {label} in {region} ({completed[0]}/{total_tasks})',
                )

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [
                executor.submit(scan_and_collect, key, fn, label)
                for key, label, fn in REGIONAL_SCANNERS
            ]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    logger.error('Region scan error: %s', e)
                    with results_lock:
                        completed[0] += 1
                        _update_status(progress=completed[0])

        for key, label, scanner_fn in GLOBAL_SCANNERS:
            try:
                _update_status(message=f'Scanning {label}…')
                global_results[key] = scanner_fn(session)
            except Exception as e:
                logger.error('Global scan error %s: %s', label, e)
            completed[0] += 1
            _update_status(progress=completed[0])

        with _status_lock:
            for key, _, _ in REGIONAL_SCANNERS:
                existing = _scan_results.get(key, [])
                kept = [r for r in existing if r.get('region') != region]
                kept.extend(region_results[key])
                _scan_results[key] = kept
            for key, _, _ in GLOBAL_SCANNERS:
                _scan_results[key] = global_results[key]

        total_resources = sum(
            len(v) for k, v in _scan_results.items()
            if not k.startswith('_') and isinstance(v, list)
        )
        _update_status(
            running=False, completed=True,
            message=f'Region scan complete — {region} refreshed. {total_resources:,} total resources.',
            completed_at=datetime.utcnow().isoformat(),
        )

    except Exception as e:
        logger.error('Region scan failed: %s', e, exc_info=True)
        _update_status(running=False, completed=False, error=str(e), message=f'Region scan failed: {e}')


def run_scan(credentials):
    session = boto3.Session(
        aws_access_key_id=credentials['access_key'],
        aws_secret_access_key=credentials['secret_key'],
        aws_session_token=credentials.get('session_token'),
    )

    _update_status(
        running=True,
        completed=False,
        progress=0,
        total=0,
        message='Getting enabled regions…',
        error=None,
        started_at=datetime.utcnow().isoformat(),
        completed_at=None,
    )

    try:
        regions = get_enabled_regions(session)

        account_id = ''
        try:
            account_id = session.client('sts').get_caller_identity()['Account']
        except Exception:
            pass

        total_tasks = len(regions) * len(REGIONAL_SCANNERS) + len(GLOBAL_SCANNERS)
        _update_status(total=total_tasks, message=f'Scanning {len(regions)} regions…')

        new_results = {k: [] for k, _, _ in REGIONAL_SCANNERS}
        new_results.update({k: [] for k, _, _ in GLOBAL_SCANNERS})
        results_lock = threading.Lock()
        completed = [0]

        def scan_and_collect(region, key, scanner_fn, label):
            items = scanner_fn(session, region)
            with results_lock:
                new_results[key].extend(items)
                completed[0] += 1
                _update_status(
                    progress=completed[0],
                    message=f'Scanned {label} in {region} ({completed[0]}/{total_tasks})',
                )

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [
                executor.submit(scan_and_collect, region, key, fn, label)
                for key, label, fn in REGIONAL_SCANNERS
                for region in regions
            ]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    logger.error('Regional scan error: %s', e)
                    with results_lock:
                        completed[0] += 1
                        _update_status(progress=completed[0])

        for key, label, scanner_fn in GLOBAL_SCANNERS:
            try:
                _update_status(message=f'Scanning {label}…')
                new_results[key] = scanner_fn(session)
            except Exception as e:
                logger.error('Global scan error %s: %s', label, e)
            completed[0] += 1
            _update_status(progress=completed[0])

        new_results['_regions'] = regions
        new_results['_account_id'] = account_id

        total_resources = sum(len(v) for k, v in new_results.items() if not k.startswith('_'))
        with _status_lock:
            _scan_results.clear()
            _scan_results.update(new_results)

        _update_status(
            running=False,
            completed=True,
            message=f'Scan complete — {total_resources:,} resources across {len(regions)} regions.',
            completed_at=datetime.utcnow().isoformat(),
        )

    except Exception as e:
        logger.error('Scan failed: %s', e, exc_info=True)
        _update_status(
            running=False,
            completed=False,
            error=str(e),
            message=f'Scan failed: {e}',
        )
