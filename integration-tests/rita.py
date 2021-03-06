#!/usr/bin/python3

from pprint import pprint
from termcolor import colored

import errno
import json
import os
import random
import re
import shlex
import signal
import subprocess
import sys
import time
import toml

NETWORK_LAB = os.path.join(os.path.dirname(__file__), 'deps/network-lab/network-lab.sh')
BABELD = os.path.join(os.path.dirname(__file__), 'deps/babeld/babeld')

RITA_DEFAULT = os.path.join(os.path.dirname(__file__), '../target/debug/rita')
RITA_EXIT_DEFAULT = os.path.join(os.path.dirname(__file__), '../target/debug/rita_exit')
BOUNTY_HUNTER_DEFAULT = os.path.join(os.path.dirname(__file__), '../target/debug/bounty_hunter')

# Envs for controlling compat testing
RITA_A = os.getenv('RITA_A', RITA_DEFAULT)
RITA_EXIT_A = os.getenv('RITA_EXIT_A', RITA_EXIT_DEFAULT)
BOUNTY_HUNTER_A = os.getenv('BOUNTY_HUNTER_A', BOUNTY_HUNTER_DEFAULT)
DIR_A = os.getenv('DIR_A', 'althea_rs_a')
RITA_B = os.getenv('RITA_B', RITA_DEFAULT)
RITA_EXIT_B = os.getenv('RITA_EXIT_B', RITA_EXIT_DEFAULT)
BOUNTY_HUNTER_B = os.getenv('BOUNTY_HUNTER_B', BOUNTY_HUNTER_DEFAULT)
DIR_B = os.getenv('DIR_B', 'althea_rs_b')

# Current binary paths (They change to *_A or *_B depending on which node is
# going to be run at a given moment, according to the layout)
RITA = RITA_DEFAULT
RITA_EXIT = RITA_EXIT_DEFAULT
BOUNTY_HUNTER = BOUNTY_HUNTER_DEFAULT

# COMPAT_LAYOUTS[None] sets everything to *_A
COMPAT_LAYOUT = os.getenv('COMPAT_LAYOUT', None)

BACKOFF_FACTOR = float(os.getenv('BACKOFF_FACTOR', 1))
CONVERGENCE_DELAY = float(os.getenv('CONVERGENCE_DELAY', 50))
DEBUG = os.getenv('DEBUG') is not None
INITIAL_POLL_INTERVAL = float(os.getenv('INITIAL_POLL_INTERVAL', 1))
PING6 = os.getenv('PING6', 'ping6')
VERBOSE = os.getenv('VERBOSE', None)

TEST_PASSES = True

abspath = os.path.abspath(__file__)
dname = os.path.dirname(abspath)
os.chdir(dname)

EXIT_SETTINGS = {
    "exits": {
        "exit_a": {
            "id": {
                "mesh_ip": "fd00::5",
                "eth_address": "0x0101010101010101010101010101010101010101",
                "wg_public_key": "fd00::5",
            },
            "registration_port": 4875,
            "state": "New"
        }
    },
    "current_exit": "exit_a",
    "wg_listen_port": 59999,
    "reg_details": {
        "zip_code": "1234",
        "email": "1234@gmail.com"
    }
}

EXIT_SELECT = {
    "exits": {
        "exit_a": {
            "state": "Registering",
        }
    },
}

COMPAT_LAYOUTS = {
        None: ['a'] * 7,  # Use *_A binaries for every node
        'old_exit': ['a'] * 4 + ['b'] + ['a'] * 2,  # The exit sports Rita B
        'new_exit': ['b'] * 4 + ['a'] + ['b'] * 2,  # Like above but opposite
        'inner_ring_old': ['a', 'b', 'b', 'a', 'a', 'b', 'b'],
        'inner_ring_new': ['b', 'a', 'a', 'b', 'b', 'a', 'a'],
        'random':   None,  # Randomize revisions used (filled at runtime)
        }

def exec_or_exit(command, blocking=True, delay=0.01):
    """
    Executes a command and terminates the program if it fails.

    :param str command: A string containing the command to run
    :param bool blocking: Decides whether to block until :data:`command` exits
    :param float delay: How long to wait before obtaining the return value
    (useful in non-blocking mode where e.g. a ``cat`` command with a
    non-existent file would very likely fail before, say, 100ms pass)
    """
    process = subprocess.Popen(shlex.split(command))

    time.sleep(delay)

    if not blocking:
        # If it didn't fail yet we get a None
        retval = process.poll() or 0
    else:
        retval = process.wait()

    if retval != 0:
        try:
            errname = errno.errorcode[retval]
        except KeyError: # The error code doesn't have a canonical name
            errname = '<unknown>'
        print('Command "{c}" failed: "{strerr}" (code {rv})'.format(
            c=command,
            strerr=os.strerror(retval), # strerror handles unknown errors gracefuly
            rv=errname,
            file=sys.stderr
            )
            )
        sys.exit(retval)


def cleanup():
    os.system("rm -rf *.db *.log *.pid private-key* mail")
    os.system("mkdir mail")
    os.system("sync")
    os.system("killall babeld rita rita_exit bounty_hunter iperf")  # TODO: This is very inconsiderate


def teardown():
    os.system("rm -rf *.pid private-key*")
    os.system("sync")
    os.system("killall babeld rita rita_exit bounty_hunter iperf")  # TODO: This is very inconsiderate


class Node:
    def __init__(self, id, fwd_price):
        self.id = id
        self.fwd_price = fwd_price
        self.neighbors = []
        self.revision = COMPAT_LAYOUTS[COMPAT_LAYOUT][self.id - 1]

    def add_neighbor(self, id):
        if id not in self.neighbors:
            self.neighbors.append(id)

    def get_interfaces(self):
        interfaces = ""
        for i in range(len(self.neighbors)):
            interfaces += "wg{} ".format(i)
        return interfaces

    def get_veth_interfaces(self):
        interfaces = []
        for i in self.neighbors:
            interfaces.append("veth-{}-{}".format(self.id, i))
        return interfaces

    def has_route(self, dest, price, next_hop, backlog=5000, verbose=False):
        """
        This function takes :data:`self` and returns ``True`` if a specified
        route is installed in the last :data:`backlog` characters of the node's
        Babel log file.

        :param Node dest: Who the route goes to
        :param int price: What the route costs
        :param Node next_hop_ip: Who's the next hop
        :param int backlog: How big chunk from the end to use for dump matching

        :rtype bool: Whether the requested route was found
        """
        buf = None
        fname = 'babeld-n{}.log'.format(self.id)
        with open(fname, 'r') as f:

            flen = f.seek(0, 2)  # Go to the end
            f.seek((flen - backlog) if backlog < flen else 0)
            buf = f.read()

        last_dump_pat = re.compile(r'.*(My id .*)', re.S | re.M)
        last_dump_match = last_dump_pat.match(buf)
        if last_dump_match is None:
            if verbose:
                print('Could not find the last dump ({}) in {}'
                      .format(last_dump_pat, fname),
                      file=sys.stderr)
            return False

        last_dump = last_dump_match.group(1)
        route_pat = re.compile(r'fd00::{d}.*price {p}.*fee {f}.*neigh fe80::{nh}.*(installed)'
                               .format(
                                       d=dest.id,
                                       p=price,
                                       f=self.fwd_price,
                                       nh=next_hop.id
                                       ))

        if route_pat.search(last_dump) is None:
            if verbose:
                print('{} not found in {}:\n{}'.format(route_pat, fname,
                      last_dump),
                      file=sys.stderr)
            return False

        return True



class Connection:
    def __init__(self, a, b):
        self.a = a
        self.b = b

    def canonicalize(self):
        if self.a.id > self.b.id:
            t = self.b
            self.b = self.a
            self.a = t

def prep_netns(id):
    exec_or_exit("ip netns exec netlab-{} sysctl -w net.ipv4.ip_forward=1".format(id))
    exec_or_exit("ip netns exec netlab-{} sysctl -w net.ipv6.conf.all.forwarding=1".format(id))
    exec_or_exit("ip netns exec netlab-{} ip link set up lo".format(id))



def start_babel(node):
    exec_or_exit(
            (
                "ip netns exec netlab-{id} {babeld_path} " +
                "-I babeld-n{id}.pid " +
                "-d 1 " +
                "-r " +
                "-L babeld-n{id}.log " +
                "-H 1 " +
                "-F {price} " +
                "-a 0 " +
                "-G 6872 " +
                '-C "default enable-timestamps true" ' +
                '-C "default update-interval 1" ' +
                "-w lo"
            ).format(babeld_path=BABELD, ifaces=node.get_interfaces(), id=node.id, price=node.fwd_price),
            blocking=False
        )


def start_bounty(id):
    os.system(
        '(RUST_BACKTRACE=full ip netns exec netlab-{id} {bounty} & echo $! > bounty-n{id}.pid) | grep -Ev "<unknown>|mio" > bounty-n{id}.log &'.format(
            id=id, bounty=BOUNTY_HUNTER))


def get_rita_defaults():
    return toml.load(open("../settings/default.toml"))


def get_rita_exit_defaults():
    return toml.load(open("../settings/default_exit.toml"))


def save_rita_settings(id, x):
    file = open("rita-settings-n{}.toml".format(id), "w")
    toml.dump(x, file)
    file.flush()
    os.fsync(file)
    file.close()
    os.system("sync")
    pass


def get_rita_settings(id):
    return toml.load(open("rita-settings-n{}.toml".format(id)))

def switch_binaries(node_id):
    """
    Switch the Rita, exit Rita and bounty hunter binaries assigned to node with ID
    :data:`node_id`.

    :param int node_id: Node ID for which we're changing binaries
    """
    global RITA, RITA_EXIT, BOUNTY_HUNTER
    if VERBOSE:
        print(("Previous binary paths:\nRITA:\t\t{}\nRITA_EXIT:\t{}\n" +
            "BOUNTY_HUNTER:\t{}").format(RITA, RITA_EXIT, BOUNTY_HUNTER))

    release = COMPAT_LAYOUTS[COMPAT_LAYOUT][node_id - 1]

    if release == 'a':
        if VERBOSE:
            print("Using A for node {}...".format(node_id))
        RITA = RITA_A
        RITA_EXIT = RITA_EXIT_A
        BOUNTY_HUNTER = BOUNTY_HUNTER_A
    elif release == 'b':
        if VERBOSE:
            print("Using B for node {}...".format(node_id))
        RITA = RITA_B
        RITA_EXIT = RITA_EXIT_B
        BOUNTY_HUNTER = BOUNTY_HUNTER_B
    else:
        print("Unknown revision kind \"{}\" for node {}".format(release, node_id))
        sys.exit(1)

    if VERBOSE:
        print(("New binary paths:\nRITA:\t\t{}\nRITA_EXIT:\t{}\n" +
            "BOUNTY_HUNTER:\t{}").format(RITA, RITA_EXIT, BOUNTY_HUNTER))

def register_to_exit(node):
    id = node.id
    os.system("ip netns exec netlab-{id} curl -XPOST 127.0.0.1:4877/settings -H 'Content-Type: application/json' -i -d '{data}'"
              .format(id=id, data=json.dumps({"exit_client": EXIT_SELECT})))

def email_verif(node):
    id = node.id

    email_text = read_email(node)

    code = re.search(r"\[([0-9]+)\]", email_text).group(1)

    print(code)

    os.system("ip netns exec netlab-{id} curl -XPOST 127.0.0.1:4877/settings -H 'Content-Type: application/json' -i -d '{data}'"
              .format(id=id, data=json.dumps({"exit_client": {"exits": {"exit_a": {"email_code": code}}}})))

    os.system("ip netns exec netlab-{id} curl 127.0.0.1:4877/settings"
              .format(id=id))



def read_email(node):
    id = node.id
    # TODO: this is O(n^2)
    for mail in os.listdir("mail"):
        with open(os.path.join("mail", mail)) as mail_file_handle:
            mail = json.load(mail_file_handle)
            if mail["envelope"]["forward_path"][0] == "{}@example.com".format(id):
                return ''.join(chr(i) for i in mail["message"])
    print("cannot find email for node {}".format(id))

def start_rita(node):
    id = node.id
    settings = get_rita_defaults()
    settings["network"]["own_ip"] = "fd00::{}".format(id)
    settings["network"]["wg_private_key_path"] = "{pwd}/private-key-{id}".format(id=id, pwd=dname)
    settings["network"]["peer_interfaces"] = node.get_veth_interfaces()
    save_rita_settings(id, settings)
    time.sleep(0.2)
    os.system(
        '(RUST_BACKTRACE=full RUST_LOG=TRACE ip netns exec netlab-{id} {rita} --config=rita-settings-n{id}.toml --platform=linux'
        ' 2>&1 & echo $! > rita-n{id}.pid) | '
        'grep -Ev "<unknown>|mio|tokio_core|hyper" > rita-n{id}.log &'.format(id=id, rita=RITA,
                                                                              pwd=dname)
        )
    time.sleep(1.5)

    EXIT_SETTINGS["reg_details"]["email"] = "{}@example.com".format(id)

    os.system("ip netns exec netlab-{id} curl -XPOST 127.0.0.1:4877/settings -H 'Content-Type: application/json' -i -d '{data}'"
              .format(id=id, data=json.dumps({"exit_client": EXIT_SETTINGS})))

def start_rita_exit(node):
    id = node.id
    settings = get_rita_exit_defaults()
    settings["network"]["own_ip"] = "fd00::{}".format(id)
    settings["network"]["wg_private_key_path"] = "{pwd}/private-key-{id}".format(id=id, pwd=dname)
    settings["network"]["peer_interfaces"] = node.get_veth_interfaces()
    save_rita_settings(id, settings)
    time.sleep(0.2)
    os.system(
        '(RUST_BACKTRACE=full RUST_LOG=TRACE ip netns exec netlab-{id} {rita} --config=rita-settings-n{id}.toml'
        ' 2>&1 & echo $! > rita-n{id}.pid) | '
        'grep -Ev "<unknown>|mio|tokio_core|hyper" > rita-n{id}.log &'.format(id=id, rita=RITA_EXIT,
                                                                              pwd=dname)
        )


def assert_test(x, description, verbose=True, global_fail=True):
    if verbose:
        if x:
            print(colored(" + ", "green") + "{} Succeeded".format(description))
        else:
            sys.stderr.write(colored(" + ", "red") + "{} Failed\n".format(description))

    if global_fail and not x:
        global TEST_PASSES
        TEST_PASSES = False
    return x


class World:
    def __init__(self):
        self.nodes = {}
        self.connections = {}
        self.bounty_id = None
        self.exit_id = None
        self.external = None

    def add_node(self, node):
        assert node.id not in self.nodes
        self.nodes[node.id] = node

    def add_exit_node(self, node):
        assert node.id not in self.nodes
        self.nodes[node.id] = node
        self.exit_id = node.id

    def add_external_node(self, node):
        assert node.id not in self.nodes
        self.nodes[node.id] = node
        self.external = node.id

    def add_connection(self, connection):
        connection.canonicalize()
        self.connections[(connection.a.id, connection.b.id)] = connection
        connection.a.add_neighbor(connection.b.id)
        connection.b.add_neighbor(connection.a.id)

    def set_bounty(self, bounty_id):
        self.bounty_id = bounty_id

    def to_ip(self, node):
        if self.exit_id == node.id:
            return "172.168.1.254"
        else:
            return "fd00::{}".format(node.id)

    def setup_dbs(self):
        os.system("rm -rf bounty.db exit.db")

        bounty_repo_dir = ".."
        exit_repo_dir = ".."

        bounty_index = self.bounty_id - 1
        exit_index = self.exit_id - 1

        if VERBOSE:
            print("DB setup: bounty_hunter index: {}, exit index: {}".format(bounty_index, exit_index))

        if COMPAT_LAYOUT:
            # Figure out whether release A or B was
            # assigned to the exit and
            # bounty
            layout = COMPAT_LAYOUTS[COMPAT_LAYOUT]
            if layout[bounty_index] == 'a':
                bounty_repo_dir = DIR_A
            elif layout[bounty_index] == 'b':
                bounty_repo_dir = DIR_B
            else:
                print("DB setup: Unknown release {} assigned to bounty".format(layout[bounty_index]))
                sys.exit(1)

            if layout[exit_index] == 'a':
                exit_repo_dir = DIR_A
            elif layout[exit_index] == 'b':
                exit_repo_dir = DIR_B
            else:
                print("DB setup: Unknown release {} assigned to exit".format(layout[exit_index]))
                sys.exit(1)

        # Save the current dir
        cwd = os.getcwd()

        # Go to bounty_hunter/ in the bounty's release
        os.chdir(os.path.join(cwd, bounty_repo_dir, "bounty_hunter"))
        if VERBOSE:
            print("DB setup: Entering {}/bounty_hunter".format(bounty_repo_dir))

        os.system(("rm -rf test.db " +
            "&& diesel migration run" +
            "&& cp test.db {dest}").format(dest=os.path.join(cwd, "bounty.db")))

        # Go to exit_db/ in the exit's release
        os.chdir(os.path.join(cwd, exit_repo_dir, "exit_db"))
        if VERBOSE:
            print("DB setup: Entering {}/exit_db".format(exit_repo_dir))

        os.system(("rm -rf test.db " +
            "&& diesel migration run" +
            "&& cp test.db {dest}").format(dest=os.path.join(cwd, "exit.db")))

        # Go back to where we started
        os.chdir(cwd)

    def create(self):
        cleanup()

        assert self.bounty_id
        nodes = {}
        for id in self.nodes:
            nodes[str(id)] = {"ip": "fd00::{}".format(id)}

        edges = []

        for id, conn in self.connections.items():
            edges.append({
                "nodes": ["{}".format(conn.a.id), "{}".format(conn.b.id)],
                "->": "",
                "<-": ""
            })

        network = {"nodes": nodes, "edges": edges}

        network_string = json.dumps(network)

        print("network topology: {}".format(network))

        print(NETWORK_LAB)
        proc = subprocess.Popen([NETWORK_LAB], stdin=subprocess.PIPE, universal_newlines=True)
        proc.stdin.write(network_string)
        proc.stdin.close()

        proc.wait()

        print("network-lab completed")

        for id in self.nodes:
            prep_netns(id)

        print("namespaces prepped")

        print("starting babel")

        for id, node in self.nodes.items():
            start_babel(node)

        print("babel started")

        print("Setting up rita_exit and bounty_hunter databases")
        self.setup_dbs()
        print("DB setup OK")

        print("starting bounty hunter")
        switch_binaries(self.bounty_id)
        start_bounty(self.bounty_id)
        print("bounty hunter started")

        switch_binaries(self.exit_id)
        start_rita_exit(self.nodes[self.exit_id])

        time.sleep(1)

        EXIT_SETTINGS["exits"]["exit_a"]["id"]["wg_public_key"] = get_rita_settings(self.exit_id)["network"]["wg_public_key"]

        print("starting rita")
        for id, node in self.nodes.items():
            if id != self.exit_id and id != self.external:
                switch_binaries(id)
                start_rita(node)
            time.sleep(0.5 + random.random() / 2) # wait 0.5s - 1s
            print()
        print("rita started")

    def test_reach(self, node_from, node_to):
        ping = subprocess.Popen(
            ["ip", "netns", "exec", "netlab-{}".format(node_from.id), PING6,
             "fd00::{}".format(node_to.id),
             "-c", "1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        output = ping.stdout.read().decode("utf-8")
        return "1 packets transmitted, 1 received, 0% packet loss" in output

    def test_reach_all(self, verbose=True, global_fail=True):
        for i in self.nodes.values():
            for j in self.nodes.values():
                if not assert_test(self.test_reach(i, j), "Reachability " +
                                   "from node {} ({}) to {} ({})".format(i.id,
                                       i.revision, j.id, j.revision),
                                   verbose=verbose, global_fail=global_fail):
                    return False
        return True

    def test_routes(self, verbose=True, global_fail=True):
        """
        Check the presence of all optimal routes.
        """
        result = True
        nodes = self.nodes

        # Caution: all_routes directly relies on the layout of the netlab mesh.
        #
        # The routes are organized into a dictionary with nodes as keys and
        # the expected routes as values:
        # all_routes = {
        #     <where_from>: [
        #                 (<where_to>, <price>, <next_hop>),
        #                 [...]
        #             ],
        #     [...]
        # }

        all_routes = {
                nodes[1]: [
                    (nodes[2], 50, nodes[6]),
                    (nodes[3], 60, nodes[6]),
                    (nodes[4], 75, nodes[6]),
                    (nodes[5], 60, nodes[6]),
                    (nodes[6], 0, nodes[6]),
                    (nodes[7], 50, nodes[6]),
                    ],
                nodes[2]: [
                    (nodes[1], 50, nodes[6]),
                    (nodes[3], 0, nodes[3]),
                    (nodes[4], 0, nodes[4]),
                    (nodes[5], 60, nodes[6]),
                    (nodes[6], 0, nodes[6]),
                    (nodes[7], 50, nodes[6]),
                    ],
                nodes[3]: [
                    (nodes[1], 60, nodes[7]),
                    (nodes[2], 0, nodes[2]),
                    (nodes[4], 25, nodes[2]),
                    (nodes[5], 10, nodes[7]),
                    (nodes[6], 10, nodes[7]),
                    (nodes[7], 0, nodes[7]),
                    ],
                nodes[4]: [
                    (nodes[1], 75, nodes[2]),
                    (nodes[2], 0, nodes[2]),
                    (nodes[3], 25, nodes[2]),
                    (nodes[5], 85, nodes[2]),
                    (nodes[6], 25, nodes[2]),
                    (nodes[7], 75, nodes[2]),
                    ],
                nodes[5]: [
                    (nodes[1], 60, nodes[7]),
                    (nodes[2], 60, nodes[7]),
                    (nodes[3], 10, nodes[7]),
                    (nodes[4], 85, nodes[7]),
                    (nodes[6], 10, nodes[7]),
                    (nodes[7], 0, nodes[7]),
                    ],
                nodes[6]: [
                    (nodes[1], 0, nodes[1]),
                    (nodes[2], 0, nodes[2]),
                    (nodes[3], 10, nodes[7]),
                    (nodes[4], 25, nodes[2]),
                    (nodes[5], 10, nodes[7]),
                    (nodes[7], 0, nodes[7]),
                    ],
                nodes[7]: [
                    (nodes[1], 50, nodes[6]),
                    (nodes[2], 50, nodes[6]),
                    (nodes[3], 0, nodes[3]),
                    (nodes[4], 75, nodes[6]),
                    (nodes[5], 0, nodes[5]),
                    (nodes[6], 0, nodes[6]),
                    ],
                }

        for node, routes in all_routes.items():
            for route in routes:
                desc = ("Optimal route from node {} ({}) " +
                        "to {} ({}) with next-hop {} ({}) and price {}").format(
                                node.id,
                                node.revision,
                                route[0].id,
                                route[0].revision,
                                route[2].id,
                                route[2].revision,
                                route[1])
                result = result and assert_test(node.has_route(*route,
                                                               verbose=verbose
                                                ),
                                                desc, verbose=verbose,
                                                global_fail=global_fail)
        return result

    def test_endpoints_all(self):
        for node in self.nodes.values():

            # We don't expect the exit to work the same as others
            if node.id == self.exit_id:
                # Exit-specific stuff
                continue

            print(colored("====== Endpoints for node {} ======".format(node.id), "green"))

            # /neighbors
            if VERBOSE:
                print(colored("Hitting /neighbors:", "green"))

            result = subprocess.Popen(shlex.split("ip netns exec "
                + "netlab-{} curl -sfg6 [::1]:4877/neighbors".format(node.id)),
                stdout=subprocess.PIPE)
            assert_test(not result.wait(), "curl-ing /neighbors")
            try:
                print("Received neighbors:")
                if VERBOSE:
                    neighbors = json.loads(result.stdout.read().decode('utf-8'))
                    pprint(neighbors)
                else:
                    print(result.stdout.read().decode('utf-8'))
            except json.JSONDecodeError as e:
                assert_test(False, "Decoding the neighbors JSON")

            # /exits
            if VERBOSE:
                print(colored("Hitting /exits:", "green"))

            result = subprocess.Popen(shlex.split("ip netns exec "
                + "netlab-{} curl -sfg6 [::1]:4877/exits".format(node.id)),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert_test(not result.wait(), "curl-ing /exits")
            try:
                print("Received exits:")
                if VERBOSE:
                    exits = json.loads(result.stdout.read().decode('utf-8'))
                    pprint(exits)
                else:
                    print(result.stdout.read().decode('utf-8'))
            except json.JSONDecodeError as e:
                assert_test(False, "Decoding the exits JSON")

            # /info
            if VERBOSE:
                print(colored("Hitting /info:", "green"))

            result = subprocess.Popen(shlex.split("ip netns exec "
                + "netlab-{} curl -sfg6 [::1]:4877/info".format(node.id)),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert_test(not result.wait(), "curl-ing /info")
            try:
                print("Received info:")
                if VERBOSE:
                    info = json.loads(result.stdout.read().decode('utf-8'))
                    pprint(info)
                else:
                    print(result.stdout.read().decode('utf-8'))
            except json.JSONDecodeError as e:
                assert_test(False, "Decoding the info JSON")

            # /settings
            if VERBOSE:
                print(colored("Hitting /settings:", "green"))

            result = subprocess.Popen(shlex.split("ip netns exec "
                + "netlab-{} curl -sfg6 [::1]:4877/settings".format(node.id)),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert_test(not result.wait(), "curl-ing /settings")
            try:
                print("Received settings:")
                if VERBOSE:
                    settings = json.loads(result.stdout.read().decode('utf-8'))
                    pprint(settings)
                else:
                    print(result.stdout.read().decode('utf-8'))
            except json.JSONDecodeError as e:
                assert_test(False, "Decoding the settings JSON")


    def get_balances(self):
        status = subprocess.Popen(
                 ["ip", "netns", "exec", "netlab-{}".format(self.bounty_id), "curl", "-s", "-g", "-6",
                  "[::1]:8888/list"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        status.wait()
        output = status.stdout.read().decode("utf-8")
        status = json.loads(output)
        balances = {}
        for i in status:
            balances[int(i["ip"].replace("fd00::", ""))] = int(i["balance"])

        return balances

        # Code for ensuring balances add up to 0

        # while s != 0 and n < 1:
        #     status = subprocess.Popen(
        #         ["ip", "netns", "exec", "netlab-{}".format(self.bounty_id), "curl", "-s", "-g", "-6",
        #          "[::1]:8888/list"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        #     status.wait()
        #     output = status.stdout.read().decode("utf-8")
        #     status = json.loads(output)
        #     balances = {}
        #     s = 0
        #     m = 0
        #     for i in status:
        #         balances[int(i["ip"].replace("fd00::", ""))] = int(i["balance"])
        #         s += int(i["balance"])
        #         m += abs(int(i["balance"]))
        #     n += 1
        #     time.sleep(0.5)
        #     print("time {}, value {}".format(n, s))

        # print("tried {} times".format(n))
        # print("sum = {}, magnitude = {}, error = {}".format(s, m, abs(s) / m))
        # assert_test(s == 0 and m != 0, "Conservation of balance")

    def gen_traffic(self, from_node, to_node, bytes):
        if from_node.id == self.exit_id:
            server = subprocess.Popen(
                ["ip", "netns", "exec", "netlab-{}".format(from_node.id), "iperf3", "-s", "-V"])
            time.sleep(0.1)
            client = subprocess.Popen(
                ["ip", "netns", "exec", "netlab-{}".format(to_node.id), "iperf3", "-c",
                 self.to_ip(from_node), "-V", "-n", str(bytes), "-Z", "-R"])

        else:
            server = subprocess.Popen(
                ["ip", "netns", "exec", "netlab-{}".format(to_node.id), "iperf3", "-s", "-V"])
            time.sleep(0.1)
            client = subprocess.Popen(
                ["ip", "netns", "exec", "netlab-{}".format(from_node.id), "iperf3", "-c",
                 self.to_ip(to_node), "-V", "-n", str(bytes), "-Z"])

        client.wait()
        time.sleep(0.1)
        server.send_signal(signal.SIGINT)
        server.wait()

    def test_traffic(self, from_node, to_node, results):
        print("Test traffic...")
        t1 = self.get_balances()
        self.gen_traffic(from_node, to_node, 1e8)
        time.sleep(30)

        t2 = self.get_balances()
        print("balance change from {}->{}:".format(from_node.id, to_node.id))
        diff = traffic_diff(t1, t2)
        print(diff)

        for node_id, balance in results.items():
            assert_test(fuzzy_traffic(diff[node_id], balance * 1e8),
                        "Balance of {} ({})".format(node_id,
                            self.nodes[node_id].revision))


def traffic_diff(a, b):
    print(a, b)
    return {key: b[key] - a.get(key, 0) for key in b.keys()}


def fuzzy_traffic(a, b):
    retval = b - 5e8 - abs(a) * 0.1 < a < b + 5e8 + abs(a) * 0.1
    if VERBOSE is not None:
        print('fuzzy_traffic({a}, {b}) is {retval}'.format(a=a, b=b,
              retval=retval))
        print(('Expression: {b} - 5e8 - abs({a}) * 0.1 < {a} < {b} + 5e8 + ' +
               'abs({a}) * 0.1').format(a=a, b=b))

    return retval


def check_log_contains(f, x):
    if x in open(f).read():
        return True
    else:
        return False


def main():
    COMPAT_LAYOUTS["random"] = ['a' if random.randint(0, 1) else 'b' for _ in range(7)]

    if VERBOSE:
        print("Random compat test layout: {}".format(COMPAT_LAYOUTS["random"]))

    a1 = Node(1, 10)
    b2 = Node(2, 25)
    c3 = Node(3, 60)
    d4 = Node(4, 10)
    e5 = Node(5, 0)
    f6 = Node(6, 50)
    g7 = Node(7, 10)
    # h8 = Node(8, 0)

    # Note: test_routes() relies heavily on this node and price layout not to
    # change. If you need to alter the test mesh, please update test_routes()
    # accordingly
    world = World()
    world.add_node(a1)
    world.add_node(b2)
    world.add_node(c3)
    world.add_node(d4)
    world.add_exit_node(e5)
    world.add_node(f6)
    world.add_node(g7)
    # world.add_external_node(h8)

    world.add_connection(Connection(a1, f6))
    world.add_connection(Connection(f6, g7))
    world.add_connection(Connection(c3, g7))
    world.add_connection(Connection(b2, c3))
    world.add_connection(Connection(b2, f6))
    world.add_connection(Connection(b2, d4))
    world.add_connection(Connection(e5, g7))
    # world.add_connection(Connection(e5, h8))

    world.set_bounty(3)  # TODO: Who should be the bounty hunter?

    world.create()

    print("Waiting for network to stabilize")
    start_time = time.time()

    interval = INITIAL_POLL_INTERVAL

    # While we're before convergence deadline
    while (time.time() - start_time) <= CONVERGENCE_DELAY:
        all_reachable = world.test_reach_all(verbose=False, global_fail=False)
        routes_ok = world.test_routes(verbose=False, global_fail=False)
        if all_reachable and routes_ok:
            break      # We converged!
        time.sleep(interval)  # Let's check again after a delay
        interval *= BACKOFF_FACTOR
        if VERBOSE is not None:
            print("%.2fs/%.2fs (going to sleep for %.2fs)" %
                  (time.time() - start_time, CONVERGENCE_DELAY, interval))

    print("Test reachabibility and optimum routes...")

    duration = time.time() - start_time

    # Test (and fail if necessary) for real and print stats on success
    if world.test_reach_all() and world.test_routes():
        print(("Converged in " + colored("%.2f seconds", "green")) % duration)
    else:
        print(("No convergence after more than " +
        colored("%d seconds", "red") +
        ", quitting...") % CONVERGENCE_DELAY)
        sys.exit(1)

    print("Waiting for clients to get info from exits")
    time.sleep(5)

    for k, v in world.nodes.items():
        if k != world.exit_id:
            register_to_exit(v)

    print("waiting for emails to be sent")
    time.sleep(16)

    for k, v in world.nodes.items():
        if k != world.exit_id:
            email_verif(v)

    world.test_endpoints_all()

    if DEBUG:
        print("Debug mode active, examine the mesh and press y to continue " +
              "with the tests or anything else to exit")
        choice = input()
        if choice != 'y':
            sys.exit(0)

    world.test_traffic(c3, f6, {
        1: 0,
        2: 0,
        3: -10 * 1.05,
        4: 0,
        5: 0,
        6: 0 * 1.05,
        7: 10 * 1.05
    })

    world.test_traffic(d4, a1, {
        1: 0 * 1.05,
        2: 25 * 1.05,
        3: 0,
        4: -75 * 1.05,
        5: 0,
        6: 50 * 1.05,
        7: 0
    })

    world.test_traffic(a1, c3, {
        1: -60 * 1.05,
        2: 0,
        3: 0,
        4: 0,
        5: 0,
        6: 50 * 1.05,
        7: 10 * 1.05
    })

    world.test_traffic(d4, e5, {
        1: 0,
        2: 25 * 1.1,
        3: 0,
        4: -135 * 1.1,
        5: 50 * 1.1,
        6: 50 * 1.1,
        7: 10 * 1.1
    })

    world.test_traffic(e5, d4, {
        1: 0,
        2: 25 * 1.1,
        3: 0,
        4: -135 * 1.1,
        5: 50 * 1.1,
        6: 50 * 1.1,
        7: 10 * 1.1
    })

    world.test_traffic(c3, e5, {
        1: 0,
        2: 0,
        3: -60 * 1.1,
        4: 0,
        5: 50 * 1.1,
        6: 0,
        7: 10 * 1.1
    })

    world.test_traffic(e5, c3, {
        1: 0,
        2: 0,
        3: -60 * 1.1,
        4: 0,
        5: 50 * 1.1,
        6: 0,
        7: 10 * 1.1
    })

    world.test_traffic(g7, e5, {
        1: 0,
        2: 0,
        3: 0,
        4: 0,
        5: 50 * 1.1,
        6: 0,
        7: -50 * 1.1
    })

    world.test_traffic(e5, g7, {
        1: 0,
        2: 0,
        3: 0,
        4: 0,
        5: 50 * 1.1,
        6: 0,
        7: -50 * 1.1
    })

    print("Check that tunnels have not been suspended")

    assert_test(not check_log_contains("rita-n1.log", "debt is below close threshold"), "Suspension of 1 (A)")
    assert_test(not check_log_contains("rita-n2.log", "debt is below close threshold"), "Suspension of 2 (B)")
    assert_test(not check_log_contains("rita-n3.log", "debt is below close threshold"), "Suspension of 3 (C)")
    assert_test(not check_log_contains("rita-n4.log", "debt is below close threshold"), "Suspension of 4 (D)")
    assert_test(not check_log_contains("rita-n6.log", "debt is below close threshold"), "Suspension of 6 (F)")
    assert_test(not check_log_contains("rita-n7.log", "debt is below close threshold"), "Suspension of 7 (G)")

    if DEBUG:
        print("Debug mode active, examine the mesh after tests and press " +
              "Enter to exit")
        input()

    teardown()

    print("done... exiting")

    if TEST_PASSES:
        print("All Rita tests passed!!")
        exit(0)
    else:
        print("Rita tests have failed :(")
        exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt as e:
        print("Received interrupt, exitting")
        teardown()
