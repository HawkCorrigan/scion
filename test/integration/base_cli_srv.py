#!/usr/bin/python3
# Copyright 2016 ETH Zurich
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
:mod:`base_cli_srv` --- Base classes for single packet end2end testing
======================================================================
"""
# Stdlib
import argparse
import copy
import logging
import os
import random
import socket
import struct
import sys
import threading
import time
from abc import ABCMeta, abstractmethod

# SCION
from endhost.sciond import SCIONDaemon
from lib.defines import AS_LIST_FILE, GEN_PATH
from lib.log import init_logging
from lib.main import main_wrapper
from lib.packet.host_addr import (
    haddr_get_type,
    haddr_parse_interface,
)
from lib.packet.packet_base import PayloadRaw
from lib.packet.path import SCIONPath
from lib.packet.scion import SCIONL4Packet, build_base_hdrs
from lib.packet.scion_addr import ISD_AS, SCIONAddr
from lib.packet.scion_udp import SCIONUDPHeader
from lib.socket import DispatcherSocket, UDPSocket
from lib.thread import kill_self, thread_safety_net
from lib.types import AddrType
from lib.util import (
    Raw,
    handle_signals,
    load_yaml_file,
)

API_TOUT = 15


class TestBase(object, metaclass=ABCMeta):
    def __init__(self, sd, data, finished, addr):
        self.sd = sd
        self.data = data
        self.finished = finished
        self.addr = addr
        self.sock = DispatcherSocket(addr, 0)
        self.sock.settimeout(1.0)
        self.success = None

    @abstractmethod
    def run(self):
        raise NotImplementedError

    def _recv(self):
        try:
            packet = self.sock.recv()[0]
        except socket.timeout:
            return None
        return SCIONL4Packet(packet)

    def _send_pkt(self, spkt, next_=None):
        next_hop, port = next_ or self.sd.get_first_hop(spkt)
        assert next_hop is not None
        logging.debug("Sending (via %s:%s):\n%s", next_hop, port, spkt)
        self.sock.send(spkt.pack(), (next_hop, port))

    def _shutdown(self):
        self.sock.close()


class TestClientBase(TestBase):
    """
    Base client app
    """
    def __init__(self, sd, data, finished, addr, dst, dport, api=True):
        super().__init__(sd, data, finished, addr)
        self.dst = dst
        self.dport = dport
        self.api = api
        self.path = None
        self.iflist = []
        self._get_path(api)

    def _get_path(self, api):
        if api:
            self._get_path_via_api()
        else:
            self._get_path_direct()
        assert self.path.mtu

    def _get_path_via_api(self):
        """
        Test local API.
        """
        data = self._try_sciond_api()
        path_len = data.pop(1) * 8
        self.path = SCIONPath(data.pop(path_len))
        haddr_type = haddr_get_type("IPV4")
        data.pop(haddr_type.LEN)  # first hop, unused here
        data.pop(2)  # port number, unused here
        self.path.mtu = struct.unpack("!H", data.pop(2))[0]
        ifcount = data.pop(1)
        for i in range(ifcount):
            isd_as = ISD_AS(data.pop(ISD_AS.LEN))
            ifid = struct.unpack("!H", data.pop(2))[0]
            self.iflist.append((isd_as, ifid))

    def _try_sciond_api(self):
        sock = UDPSocket(bind=("127.0.0.1", 0), addr_type=AddrType.IPV4)
        msg = b'\x00' + self.dst.isd_as.pack()
        start = time.time()
        while time.time() - start < API_TOUT:
            addr = self.sd.api_addr
            port = self.sd.api_port
            logging.debug("Sending path request to local API (%s:%s)",
                          addr, port)
            sock.send(msg, (addr, port))
            data = Raw(sock.recv()[0], "Path response")
            if data:
                sock.close()
                return data
            logging.debug("Empty response from local api.")
        logging.critical("Unable to get path from local api.")
        sock.close()
        kill_self()

    def _get_path_direct(self, flags=0):
        logging.debug("Sending PATH request for %s", self.dst)
        # Get paths through local API.
        paths = []
        for _ in range(5):
            paths = self.sd.get_paths(self.dst.isd_as, flags=flags)
            if paths:
                break
        else:
            logging.critical("Unable to get path directly from sciond")
            kill_self()
        self.path = paths[0]
        self._get_iflist()

    def _get_iflist(self):
        self.iflist = self.path.interfaces

    def run(self):
        while not self.finished.is_set():
            self._send()
            spkt = self._recv()
            if not spkt or not self._handle_response(spkt):
                if not spkt:
                    logging.error("Timeout waiting for response")
                self.success = False
                self.finished.set()
        self._shutdown()

    def _send(self):
        self._send_pkt(self._build_pkt())
        if self.iflist:
            logging.debug("Interfaces: %s", ", ".join(
                ["%s:%s" % ifentry for ifentry in self.iflist]))

    def _build_pkt(self, path=None):
        cmn_hdr, addr_hdr = build_base_hdrs(self.addr, self.dst)
        l4_hdr = self._create_l4_hdr()
        extensions = self._create_extensions()
        if path is None:
            path = self.path
        spkt = SCIONL4Packet.from_values(
            cmn_hdr, addr_hdr, path, extensions, l4_hdr)
        spkt.set_payload(self._create_payload(spkt))
        spkt.update()
        return spkt

    def _get_first_hop(self, spkt):
        return self.sd.get_first_hop(spkt)

    def _create_payload(self, spkt):
        return PayloadRaw(self.data)

    def _create_l4_hdr(self):
        return SCIONUDPHeader.from_values(
            self.addr, self.sock.port, self.dst, self.dport)

    def _create_extensions(self):
        return []

    @abstractmethod
    def _handle_response(self, spkt):
        raise NotImplementedError


class TestServerBase(TestBase):
    """
    Base server app
    """
    def run(self):
        while not self.finished.is_set():
            spkt = self._recv()
            if spkt and not self._handle_request(spkt):
                self.success = False
                self.finished.set()
        self._shutdown()

    @abstractmethod
    def _handle_request(self, spkt):
        raise NotImplementedError


class TestClientServerBase(object):
    """
    Test module to run client and server
    """
    def __init__(self, client, server, sources, destinations, local=True):
        self.client_ip = haddr_parse_interface(client)
        self.server_ip = haddr_parse_interface(server)
        self.src_ias = sources
        self.dst_ias = destinations
        self.local = local
        self.scionds = {}

    def run(self):
        """
        Run a test for every pair of src and dst
        """
        for src_ia in self.src_ias:
            for dst_ia in self.dst_ias:
                if not self.local and src_ia == dst_ia:
                    continue
                self._run_test(src_ia, dst_ia)

    def _run_test(self, src_ia, dst_ia):
        """
        Run client and server, wait for both to finish
        """
        logging.info("Testing: %s -> %s", src_ia, dst_ia)
        # finished is used by the client/server to signal to the other that they
        # are stopping.
        finished = threading.Event()
        src_addr = SCIONAddr.from_values(src_ia, self.client_ip)
        dst_addr = SCIONAddr.from_values(dst_ia, self.server_ip)
        data = self._create_data()
        server = self._create_server(data, finished, dst_addr)
        client = self._create_client(data, finished, src_addr, dst_addr,
                                     server.sock.port)
        server_name = "Server %s" % dst_ia
        s_thread = threading.Thread(
            target=thread_safety_net, args=(server.run,), name=server_name,
            daemon=True)
        s_thread.start()
        client.run()
        # If client is finished, server should finish within ~1s (due to recv
        # timeout). If it hasn't, then there was a problem.
        s_thread.join(2.0)
        if s_thread.is_alive():
            logging.error("Timeout waiting for server thread to terminate")
            sys.exit(1)
        if not client.success and server.success:
            logging.error("Client success? %s Server success? %s",
                          client.success, server.success)
            sys.exit(1)
        logging.debug("Success")

    def _create_data(self):
        """
        Create raw payload data
        """
        return b""

    def _create_server(self, data, finished, addr):
        """
        Instantiate server app
        """
        return TestServerBase(self._run_sciond(addr), data, finished, addr)

    def _create_client(self, data, finished, src, dst, port):
        """
        Instantiate client app
        """
        return TestClientBase(self._run_sciond(src), data, finished, src, dst,
                              port)

    def _run_sciond(self, addr):
        if addr.isd_as not in self.scionds:
            logging.debug("Starting sciond for %s", addr.isd_as)
            # Local api on, random port, random api port
            self.scionds[addr.isd_as] = start_sciond(addr, api=True)
        return self.scionds[addr.isd_as]


def start_sciond(addr, api=False, port=0, api_addr=None, api_port=0):
    conf_dir = "%s/ISD%d/AS%d/endhost" % (
        GEN_PATH, addr.isd_as[0], addr.isd_as[1])
    return SCIONDaemon.start(
        conf_dir, addr.host, api_addr=api_addr, run_local_api=api, port=port,
        api_port=api_port)


def _load_as_list():
    as_dict = load_yaml_file(os.path.join(GEN_PATH, AS_LIST_FILE))
    as_list = []
    for as_str in as_dict.get("Non-core", []) + as_dict.get("Core", []):
        as_list.append(ISD_AS(as_str))
    return as_list


def _parse_locs(as_str, as_list):
    if as_str:
        return [ISD_AS(as_str)]
    copied = copy.copy(as_list)
    random.shuffle(copied)
    return copied


def setup_main(name):
    handle_signals()
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--client', help='Client address')
    parser.add_argument('-s', '--server', help='Server address')
    parser.add_argument('-m', '--mininet', action='store_true',
                        help="Running under mininet")
    parser.add_argument('src_ia', nargs='?', help='Src isd-as')
    parser.add_argument('dst_ia', nargs='?', help='Dst isd-as')
    args = parser.parse_args()
    init_logging("logs/%s" % name, console_level=logging.INFO)

    if not args.client:
        args.client = "169.254.0.2" if args.mininet else "127.0.0.2"
    if not args.server:
        args.server = "169.254.0.3" if args.mininet else "127.0.0.3"
    as_list = _load_as_list()
    srcs = _parse_locs(args.src_ia, as_list)
    dsts = _parse_locs(args.dst_ia, as_list)
    return args, srcs, dsts


def main():
    args, srcs, dsts = setup_main("base")
    TestClientServerBase(args.client, args.server, srcs, dsts).run()


if __name__ == "__main__":
    main_wrapper(main)
