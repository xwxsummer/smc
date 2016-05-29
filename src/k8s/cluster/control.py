#!/usr/bin/env python3

import os, shutil, sys, tempfile
join = os.path.join

# Where Kubernetes is installed from https://github.com/kubernetes/kubernetes/releases

KUBERNETES = join(os.environ['HOME'], 'kubernetes')
CLUSTER    = join(KUBERNETES, 'cluster')

if not os.path.exists(CLUSTER):
    print("Install Kubernetes from https://github.com/kubernetes/kubernetes/releases in {dest}.".format(dest=KUBERNETES))
    sys.exit(1)

# Boilerplate to ensure we are in the directory of this path and make the util module available.
SCRIPT_PATH = os.path.split(os.path.realpath(__file__))[0]
os.chdir(SCRIPT_PATH)
sys.path.insert(0, os.path.abspath(os.path.join(SCRIPT_PATH, '..', 'util')))
import util

"""

"""

def cost_of_cluster(node_size, node_disk_type, node_disk_size, min_nodes, max_nodes, preemptible,
                    master_size, master_disk_type, master_disk_size):
    sys.path.insert(0, os.path.abspath(os.path.join(SCRIPT_PATH, '..', '..', 'scripts', 'gce')))
    import pricing
    def show(v, period='monthly'):
        return "{low:<8} <= {period:<8} cost <= {high:<8}    ".format(
                low=pricing.money(v[0]), high=pricing.money(v[1]), period=period)
    master_cpu = pricing.cpu_cost(size=master_size, preemptible=False, region='us') # region assumed
    print("master_cpu  = ", show(master_cpu))
    node_cpu   = pricing.cpu_cost(size=node_size, preemptible=preemptible, region='us')
    print("1 node_cpu  = ", show(node_cpu))
    nodes_cpu  = [node_cpu[0]*min_nodes, node_cpu[1]*max_nodes]
    print("nodes_cpus  = ", show(nodes_cpu))
    master_disk = pricing.disk_cost(master_disk_size, master_disk_type)
    print("master_disk = ", show(master_disk))
    node_disk = pricing.disk_cost(node_disk_size, node_disk_type)
    print("1 node_disk = ", show(node_disk))
    nodes_disk = [node_disk[0]*min_nodes, node_disk[1]*max_nodes]
    print("nodes_disk  = ", show(nodes_disk))
    total = [master_cpu[0] + nodes_cpu[0] + master_disk[0] + nodes_disk[0],
             master_cpu[1] + nodes_cpu[1] + master_disk[1] + nodes_disk[1]]
    print("-"*50)
    print("total       = ", show(total))
    print("total       = ", show([total[0]/30.5, total[1]/30.5], 'daily'))

def create_cluster(args):
    if args.min_nodes > args.max_nodes:
        args.max_nodes = args.min_nodes
    if args.cost:
        c = cost_of_cluster(node_size = args.node_size,
                            node_disk_type = 'pd-ssd' if args.node_ssd else 'pd-standard',
                            node_disk_size = args.node_disk_size,
                            min_nodes = args.min_nodes,
                            max_nodes = args.max_nodes,
                            master_size = args.master_size,
                            master_disk_type = 'pd-ssd',  # forced by k8s
                            master_disk_size = args.master_disk_size,
                            preemptible = not args.non_preemptible)
        print(c)
        return

    # see https://github.com/kubernetes/kubernetes/blob/master/cluster/gce/config-default.sh for env vars
    env = {
        'KUBE_ENABLE_CLUSTER_MONITORING' : 'google',
        'KUBE_GCE_ZONE'                  : args.zone,
        'NODE_SIZE'                      : args.node_size,
        'NUM_NODES'                      : str(args.min_nodes),
        'MASTER_SIZE'                    : args.master_size,
        'MASTER_DISK_SIZE'               : "%sGB"%args.master_disk_size,
        'NODE_DISK_TYPE'                 : 'pd-ssd' if args.node_ssd else 'pd-standard',
        'NODE_DISK_SIZE'                 : "%sGB"%args.node_disk_size,
        'PREEMPTIBLE_NODE'               : 'false' if args.non_preemptible else 'true',
        'KUBE_GCE_INSTANCE_PREFIX'       : 'k8s-'+args.name,
        'KUBE_ENABLE_NODE_AUTOSCALER'    : 'true' if args.min_nodes < args.max_nodes else 'false',
        'KUBE_AUTOSCALER_MIN_NODES'      : str(args.min_nodes),
        'KUBE_AUTOSCALER_MAX_NODES'      : str(args.max_nodes)
    }

    env.update(os.environ)
    util.run(join(CLUSTER, 'kube-up.sh'), env=env)
    update_firewall()

def update_firewall():
    """
    We manually modify the firewall so that the rule default-default-internal only allows traffic
    from k8s clusters, and not everything.  By default kube-up.sh creates the firewall and allows
    traffic from everything inside the GCE project to everything... which is **BAD** since
    the compute projects would then be able to connect directly to the database, and could otherwise
    cause trouble (e.g., DOS).  Of course, the database has along random password, but still.
    So we change the tags for this firewall rule to all tags starting in k8s.
    """
    # see http://stackoverflow.com/questions/37047089/fastest-way-to-fetch-tags-and-status-of-gce-instances
    tags = []
    for x in util.run(['gcloud', 'compute', 'instances', 'list', '--format', 'table(tags.list())'], get_output=True).splitlines():
        v = x.split("items=")
        if len(v) > 1:
            for t in eval(v[-1]):
                tags.append(t)
    tags = set(tags)
    tags = [x for x in tags if x.startswith('k8s-')]
    util.run(['gcloud', 'compute', 'firewall-rules', 'update', 'default-default-internal',
              '--target-tags', ','.join(tags)])

def select_cluster(args):
    print('selecting ', args.name)
    context = "{project_name}_k8s-{name}".format(
        project_name = util.get_default_gcloud_project_name(),
        name         = args.name)
    util.run(['kubectl', 'config', 'use-context', context])

def delete_cluster(args):
    select_cluster(args)

    # IMPORTANT: shutdown all deployments *and* services first; otherwise we end up with
    # a random load balancer left laying around, which costs, and will never be used again.
    delete_all()

    env = {
        'KUBE_GCE_INSTANCE_PREFIX' : 'k8s-'+args.name,
        'KUBE_GCE_ZONE'            : args.zone
    }
    env.update(os.environ)
    util.run(join(CLUSTER, 'kube-down.sh'), env=env)

def autoscale_cluster(args):
    if args.min_nodes is not None and args.max_nodes < args.min_nodes:
        args.min_nodes = args.max_nodes
    v = ['gcloud', 'compute', 'instance-groups', 'managed', 'set-autoscaling', 'k8s-'+args.name+'-minion-group',
         '--max-num-replicas', str(args.max_nodes)]
    if args.min_nodes is not None:
        v.append('--min-num-replicas')
        v.append(str(args.min_nodes))
    if args.cpu_percent is not None:
        v.append("--scale-based-on-cpu")
        v.append("--target-cpu-utilization")
        v.append(str(args.cpu_percent/100.0))
    util.run(v)

def resize_cluster(args):
    util.run(['gcloud', 'compute', 'instance-groups', 'managed', 'resize', 'k8s-'+args.name+'-minion-group',
         '--size', str(args.size)])

def run_all():
    x = util.get_deployments()
    for name in ['rethinkdb0', 'rethinkdb-proxy', 'smc-webapp-static', 'smc-hub', 'haproxy']:
        if name not in x:
            if name == 'rethinkdb0':
                name = 'rethinkdb'
            print('\n******\nRUNNING {name}\n******\n'.format(name=name))
            util.run([join(SCRIPT_PATH,'..',name,'control.py'), 'run'])

def delete_all():
    x = util.get_deployments()
    s = util.get_services()
    for name in ['rethinkdb0', 'rethinkdb-proxy', 'smc-webapp-static', 'smc-hub', 'haproxy']:
        if name in x:
            if name == 'rethinkdb0':
                name = 'rethinkdb'
            print('\n******\nDELETING {name}\n******\n'.format(name=name))
            util.run([join(SCRIPT_PATH,'..',name,'control.py'), 'delete'])
        if name in s:
            util.run(['kubectl', 'delete', 'services', name])

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(
            description='Control deployment of Kubernetes clusters',
            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    subparsers = parser.add_subparsers(help='sub-command help')

    sub = subparsers.add_parser('create', help='create k8s cluster',
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    sub.add_argument("name",               type=str,              help="name of the cluster")
    sub.add_argument("--zone",             default="us-central1-c", help="zone of the cluster")
    sub.add_argument("--master-size",      default="n1-standard-1",    help="node VM type")
    sub.add_argument("--master-disk-size", default=10, type=int,  help="size of master disks")
    sub.add_argument("--node-size",        default="n1-standard-1",    help="node VM type")
    sub.add_argument("--node-ssd",         action="store_true",   help="use SSD's on the nodes")
    sub.add_argument("--node-disk-size",   default=30, type=int,  help="size of node disks")
    sub.add_argument("--min-nodes",        default=2, type=int,   help="min number of nodes")
    sub.add_argument("--max-nodes",        default=5, type=int,   help="max number of nodes (if >min, autoscale)")
    sub.add_argument("--non-preemptible",  action="store_true",   help="do NOT use preemptible nodes")
    sub.add_argument("--cost",             action="store_true",   help="instead of creating only estimate monthly cost of cluster")
    sub.set_defaults(func=create_cluster)

    sub = subparsers.add_parser('select', help='select a given cluster')
    sub.add_argument('name', type=str, help='name of the cluster to switch to (so is default for kubectl)')
    sub.set_defaults(func=select_cluster)

    sub = subparsers.add_parser('delete-cluster', help='delete k8s cluster')
    sub.add_argument('name', type=str, help='name of the cluster to delete')
    sub.add_argument("--zone", default="us-central1-c", help="zone of the cluster")
    sub.set_defaults(func=delete_cluster)

    sub = subparsers.add_parser('autoscale', help='autoscale the nodes')
    sub.add_argument('name', type=str, help='name of the cluster to rescale')
    sub.add_argument("--max-nodes",   type=int,     help="max number of nodes -- required and must be at least 1")
    sub.add_argument("--min-nodes",   type=int, default=None, help="minimum number of nodes")
    sub.add_argument("--cpu-percent", type=int, default=None, help="target average cpu percentage (number between 1 and 100)")
    sub.set_defaults(func=autoscale_cluster)

    sub = subparsers.add_parser('resize', help='set the number of nodes')
    sub.add_argument('name', type=str, help='name of the cluster to rescale')
    sub.add_argument("--size",  type=int, help="number of nodes", required=True)
    sub.set_defaults(func=resize_cluster)

    sub = subparsers.add_parser('run-deployments', help="starts all deployments running in the current cluster")
    sub.set_defaults(func=lambda args: run_all())

    sub = subparsers.add_parser('delete-deployments', help='delete all smc deployments (and service!) in the current cluster')
    sub.set_defaults(func=lambda args: delete_all())

    args = parser.parse_args()
    if hasattr(args, 'func'):
        args.func(args)
