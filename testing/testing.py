#!/usr/bin/env python
"""
Ethereum system-testing

    Bootstraps x number nodes cpp, go and python nodes
    TODO Ask to setup ES node
    TODO Ask to terminate nodes after each test run or after failures
    TODO Ask to clean up AMIs (previous ones just gets cleaned up on new runs)

    TODO Make a futures wrapper for better pattern reuse (in tasks.py)
"""
import os
import json
import logging
from glob import glob
from getpass import getpass
from fabric.api import settings, abort  # task, env, run, prompt, cd, get, put, runs_once, sudo
from fabric.contrib.console import confirm  # from fabric.utils import error, puts, fastprint
from tasks import machine_list, setup_es, bootstrap, launch_prepare_nodes, prepare_nodes, run_scenarios, rollback, teardown
from argparse import ArgumentParser
from . import __version__

logger = logging.getLogger(__name__)

# Get available scenarios
path = os.path.dirname(__file__)
scenarios = glob(os.path.abspath(os.path.join(path, '..', 'scenarios', 'scenario_*.py')))
available = []
for scenario in scenarios:
    available.append(scenario.split('/')[-1][9:-3])

def parse_arguments(parser):
    parser.add_argument(
        "-c", "--cpp",
        default=1,
        dest="cpp_nodes",
        type=int,
        help="Number of C++ nodes to launch (default: %(default)s)")
    parser.add_argument(
        "--cpp-image",
        dest="cpp_image",
        default="ethereum/client-cpp",
        help="Base C++ image to use (default: %(default)s)")
    parser.add_argument(
        "-g", "--go",
        default=1,
        dest="go_nodes",
        type=int,
        help="Number of Go nodes to launch (default: %(default)s)")
    parser.add_argument(
        "--go-image",
        dest="go_image",
        default="ethereum/client-go",
        help="Base Go image to use (default: %(default)s)")
    parser.add_argument(
        "-p", "--python",
        default=1,
        dest="python_nodes",
        type=int,
        help="Number of Python nodes to launch (default: %(default)s)")
    parser.add_argument(
        "--python-image",
        dest="python_image",
        default="ethereum/client-python",
        help="Base PyEthApp image to use (default: %(default)s)")
    parser.add_argument(
        "-e", "--es",
        default=None,
        dest="elasticsearch",
        help="IP of the ElasticSearch node (default: %(default)s)")
    parser.add_argument(
        "-i", "--vpc-id",
        default="vpc-3fe30e5a",
        dest="vpc",
        help="AWS VPC ID (default: %(default)s)")
    parser.add_argument(
        "-r", "--region",
        default="us-east-1",
        dest="region",
        help="AWS Region (default: %(default)s)")
    parser.add_argument(
        "-z", "--zone",
        default="b",
        dest="zone",
        help="AWS Zone (default: %(default)s)")
    parser.add_argument(
        "-d", "--debug",
        default=False,
        dest="debug",
        type=bool,
        help="Debug (default: %(default)s)")
    parser.add_argument(
        "-s", "--scenarios",
        choices=available,
        default='all',
        dest="scenarios",
        nargs="*",
        help="Scenarios to test (default: %(default)s)")
    parser.add_argument(
        "command",
        choices=["ls", "rm"],
        nargs='?',
        help="Optional commands for maintenance")
    parser.add_argument(
        "parameters",
        nargs='*',
        help="Optional parameters")

    return parser.parse_args()


class Inventory(object):
    def __init__(self):
        machines = self.parse_machines()

        self.instances = machines['instances']
        self.bootnodes = machines['bootnodes']
        self.clients = machines['clients']
        # self.roles = machines.roles
        if not machines['es']:
            try:
                with open('es.json', 'r') as f:
                    es = json.load(f)
                machines['es'] = es['ip']
            except:
                machines['es'] = None
        self.es = machines['es']

    def parse_machines(self):
        machines = machine_list().splitlines()[1:]
        parsed = {}
        instances = []
        bootnodes = []
        clients = []
        es = None

        for mach in machines:
            fields = mach.split()
            ip = fields[-1][6:-5]
            instances.append({fields[0]: ip})
            if mach.startswith('bootnode'):
                bootnodes.append(ip)
            elif mach.startswith('testnode'):
                clients.append(ip)
            elif mach.startswith('elasticsearch'):
                es = ip

        parsed['bootnodes'] = bootnodes
        parsed['clients'] = clients
        parsed['instances'] = instances
        parsed['es'] = es

        return parsed


def main():
    parser = ArgumentParser(version=__version__)
    args = parse_arguments(parser)

    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s",
            datefmt="%H:%M:%S")
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(name)s: %(message)s",
            datefmt="%H:%M:%S")

    logger.info("=====")
    logger.info("Ethereum system-testing %s", __version__)
    logger.info("=====\n")

    # Ask to setup ES node
    es = None
    if not args.elasticsearch:
        try:
            with open('es.json', 'r') as f:
                es = json.load(f)
            es = es['ip']
        except:
            if confirm("No ElasticSearch node was found, set one up?"):
                user = raw_input("Choose a username for Kibana: ")
                passwd = getpass("Choose a password: ")
                cpasswd = getpass("Confirm password: ")
                if passwd != cpasswd:
                    abort("Password doesn't match, aborting...")
                es = setup_es(args.vpc, args.region, args.zone, user, passwd)
            else:
                if confirm("Abort?"):
                    abort("Aborting...")
                else:
                    logger.warn("Running without ElasticSearch, tests will fail!")
    else:
        with open('es.json', 'w') as f:
            save_es = {'ip': args.elasticsearch}
            json.dump(save_es, f)
        es = args.elasticsearch

    # Total nodes
    total = args.cpp_nodes + args.go_nodes + args.python_nodes

    # Confirm setup parameters
    if not confirm("Setting up %s node%s (%s C++, %s Go, %s Python) in %s%s region, "
                   "logging to ElasticSearch node at %s, "
                   "running scenarios: %s. Continue?" % (
            total,
            ("s" if total > 1 else ""),
            args.cpp_nodes,
            args.go_nodes,
            args.python_nodes,
            args.region,
            args.zone,
            es,
            args.scenarios)):
        logger.warn("Aborting...")
        raise SystemExit

    # TODO per-user nodenames / tags
    clients = []
    nodenames = []
    if args.cpp_nodes:
        clients.append("cpp")
        nodenames.append("prepare-cpp")
    if args.go_nodes:
        clients.append("go")
        nodenames.append("prepare-go")
    if args.python_nodes:
        clients.append("python")
        nodenames.append("prepare-python")

    # Prepare nodes, creates new AMIs / stores IDs to file for reuse
    images = {
        'cpp': args.cpp_image,
        'go': args.go_image,
        'python': args.python_image
    }

    try:
        with open('amis.json', 'r') as f:
            ami_ids = json.load(f)
    except:
        with settings(warn_only=False):
            with rollback(nodenames):
                launch_prepare_nodes(args.vpc, args.region, args.zone, clients)
        with settings(warn_only=False):
            with rollback(nodenames):
                ami_ids = prepare_nodes(args.region, args.zone, clients=clients, images=images)

        # Teardown prepare nodes
        teardown(nodenames)

    # Set nodes object
    nodes = {'cpp': [], 'go': [], 'python': []}
    nodenames = []
    for x in xrange(0, args.cpp_nodes):
        nodes['cpp'].append("testnode-cpp-%s" % x)
    for x in xrange(0, args.go_nodes):
        nodes['go'].append("testnode-go-%s" % x)
    for x in xrange(0, args.python_nodes):
        nodes['python'].append("testnode-python-%s" % x)
    nodenames = nodes['cpp'] + nodes['go'] + nodes['python']
    logger.info("Nodes: %s" % nodes)
    logger.info("Nodenames: %s" % nodenames)

    # Bootstrap nodes using prepared AMIs from amis.json if it exists
    bootstrap(
        args.vpc,
        args.region,
        args.zone,
        ami_ids,
        nodes)

    # List machines
    machines = machine_list()
    logger.info("Machines:")
    logger.info(machines)
    logger.info("===")

    # Check inventory
    inventory = Inventory()
    logger.info('bootnodes: %s' % inventory.bootnodes)
    logger.info('elasticsearch: %s' % inventory.es)
    logger.info('clients: %s' % inventory.clients)
    logger.info('instances: %s' % inventory.instances)
    # logger.info('roles: %s' % inventory.roles)

    # Set options (daemonize and entrypoint), see clients.py
    # options = {
    #     'cpp': '-d --entrypoint eth',
    #     'go': '-d --entrypoint geth',
    #     'python': '-d --entrypoint pyethapp',
    # }

    # TODO Launch bootnodes
    # ... set commands

    # # test run
    # commands = {
    #     'cpp': '',
    #     'go': '',
    #     'python': ''
    # }
    # run_containers(nodes, images, options, commands)

    # Load scenarios
    if args.scenarios == 'all':
        load_scenarios = scenarios
    else:
        load_scenarios = []
        for scenario in args.scenarios:
            load_scenarios.append(
                os.path.abspath(os.path.join(path, '..', 'scenarios', "scenario_%s.py" % scenario)))
    logger.info("Testing %s" % load_scenarios)

    # Run scenarios
    # TODO ask to run sequentially or in parallel?
    run_scenarios(load_scenarios)

    # Teardown
    # TODO Make teardown optional / saving instance IDs to file for even quicker reuse
    teardown(nodenames)

if __name__ == '__main__':
    main()
