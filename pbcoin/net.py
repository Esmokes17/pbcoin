import asyncio
import json
import logging
from sys import getsizeof
from random import shuffle
from enum import IntEnum, auto
from hashlib import md5
from socket import *

DEFAULT_PORT = 8989
DEFAULT_HOST = '127.0.0.1'

#! log not to be here
log = logging.getLogger()
log.setLevel(logging.DEBUG)

TOTAL_NUMBER_CONNECTIONS = 2
MAX_BYTE_SIZE = 8

def sizeof(data):
    return '{:>08d}'.format(getsizeof(data))

class ConnectionCode(IntEnum):
    NEW_NEIGHBOR = auto()  # send information node as a new neighbors
    NEW_NEIGHBORS_REQUEST = auto()  # request some new nodes for neighbors
    NEW_NEIGHBORS_FIND = auto()  # declare find new neighbors ()
    NOT_NEIGHBOR = auto()  # not be neighbors anymore!

class Node:
    def __init__(self, ip, port):
        self.ip = ip
        self.port = port
        if not self.ip:
            self.ip = DEFAULT_HOST
        if not self.port:
            self.prt = DEFAULT_PORT
        self.uid = self.calculate_uid(ip, str(port)) # TODO: use public key
        self.neighbors: dict[str, tuple[str, int]] = dict()

    async def connectTo(self, dst_ip, dst_port):
        try:
            reader, writer = await asyncio.open_connection(dst_ip, int(dst_port))
            log.info(f"from {self.ip} Connect to {dst_ip}:{dst_port}")
        except ConnectionError:
            log.error("Error Connection", exc_info=True)
            raise ConnectionError
        return reader, writer

    async def connectAndSend(self, dst_ip: str, dst_port: int, data: str):
        try:
            reader, writer = await self.connectTo(dst_ip, dst_port)
            writer.write(bytes(sizeof(data).encode()))
            writer.write(bytes(data.encode()))
            await writer.drain()
            size_data = int(await reader.read(MAX_BYTE_SIZE))
            size_data = int(size_data)
            rec_data = await reader.read(size_data)
            log.debug(f'receive data from {dst_ip}:{dst_port} {rec_data.decode()}')
            writer.close()
            await writer.wait_closed()
        except ConnectionError:
            log.error("Error Connection", exc_info=True)
            raise ConnectionError
        finally:
            return rec_data

    async def listen(self):
        """start listening request from other nodes and callback handleRequest"""
        server = await asyncio.start_server(self.handleRequest, host=self.ip, port=self.port)
        if not server:
            raise ConnectionError
        log.info(f"node connection is serve on {server.sockets[0].getsockname()}")
        loop = asyncio.get_event_loop()
        async with server:
            try:
                await server.serve_forever()
            except:
                log.error("connection is lost")
            finally:
                server.close()
                await server.wait_closed()

    async def handleRequest(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """ handle requests that receive other nodes """
        size = int(await reader.read(MAX_BYTE_SIZE))
        data = await reader.read(size)
        log.debug(f'receive data: {data.decode()}')
        data = json.loads(data.decode())

        assert len(ConnectionCode) == 4, "some requests are not implemented yet!"
        type = data['type']
        if type == ConnectionCode.NEW_NEIGHBOR:
            await self.handleNewNeighbor(data)
        elif type == ConnectionCode.NEW_NEIGHBORS_REQUEST:
            await self.handleRequestNewNode(data, reader, writer)
        elif type == ConnectionCode.NEW_NEIGHBORS_FIND:
            # TODO: this type implemented in startup
            log.warn("bad request for find new node")
        elif type == ConnectionCode.NOT_NEIGHBOR:
            await self.handleNotNeighbor(data, writer)
        else:
            raise NotImplementedError
        writer.close()
        await writer.wait_closed()

    async def handleNewNeighbor(self, data):
        """ add new node to neighbors nodes """
        new_node_ip, new_node_port = data["new_node"].split(":")
        uid = data["uid"]
        self.neighbors[uid] = (new_node_ip, int(new_node_port))
        log.info(f"new neighbor for {self.ip} : {new_node_ip}")

    async def handleRequestNewNode(self, data, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        final_req = None
        n_connections = data["number_connections_requests"]
        data["passed_nodes"].append(self.ip)
        final_req = {
            "status": True,
            "uid": self.uid,
            "type": ConnectionCode.NEW_NEIGHBORS_REQUEST,
            "src_ip": self.ip,
            "dst_ip": data["src_ip"],
            "number_connections_requests": n_connections,
            "p2p_nodes": data["p2p_nodes"],
            "passed_nodes": data["passed_nodes"]
        }
        if len(self.neighbors) < TOTAL_NUMBER_CONNECTIONS: # add new neighbor to itself
            n_connections -= 1
            final_req["number_connections_requests"] = n_connections
            final_req["p2p_nodes"].append(f"{self.ip}:{self.port}")
        if n_connections != 0:
            if final_req != None:
                addr_msg = final_req.copy()
            else:
                addr_msg = data.copy()
            addr_msg['src_ip'] = self.ip
            for uid in list(self.neighbors):
                addr = self.neighbors.get(uid)
                if addr == None:
                    continue
                ip, port = addr
                # doesn't send data to repetitious node and stuck in a loop
                if ip not in addr_msg['passed_nodes']:
                    addr_msg['dst_ip'] = ip
                    try:
                        response = await self.connectAndSend(ip, port, json.dumps(addr_msg))
                        final_req = json.loads(response.decode())
                        n_connections = final_req['number_connections_requests']
                    except ConnectionError:
                        # TODO: checking for connection that neighbor is online yet?
                        log.error("", exc_info=True)
                if n_connections == 0:
                    break

        if n_connections == TOTAL_NUMBER_CONNECTIONS and len(self.neighbors) == TOTAL_NUMBER_CONNECTIONS:
            neighbors_uid = list(self.neighbors.keys())
            shuffle(neighbors_uid)
            for uid in neighbors_uid:
                ip, port = self.neighbors[uid]
                request = {
                    "status": True,
                    "type": ConnectionCode.NOT_NEIGHBOR,
                    "dst_ip": ip,
                    "src_ip": self.ip,
                    "uid": self.uid
                }
                response = await self.connectAndSend(ip, port, json.dumps(request))
                response = json.loads(response.decode())
                if response['status'] == True:
                    self.neighbors.pop(uid, None)
                    log.info(f"delete neighbor for {self.ip} : {ip}")
                    new_nodes = [f"{self.ip}:{self.port}", f"{ip}:{port}"]
                    final_req["p2p_nodes"] += new_nodes
                    final_req['number_connections_requests'] -= 2
                    break

        if final_req != None:
            final_req['src_ip'] = self.ip
            final_req['type'] = ConnectionCode.NEW_NEIGHBORS_FIND
            if final_req['number_connections_requests'] == 0:
                final_req['status'] = True
            else:
                final_req['status'] = False
        else:
            raise NotImplementedError

        writer.write(bytes(sizeof(final_req).encode()))
        writer.write(bytes(json.dumps(final_req).encode()))

    async def handleNotNeighbor(self, data, writer):
        """ delete neighbor """
        ip = data["src_ip"]
        uid = data["uid"]
        data = {
            "status": False,
            "dst_ip": ip,
            "src_ip": self.ip,
        }
        if len(self.neighbors) == TOTAL_NUMBER_CONNECTIONS:
            self.neighbors.pop(uid, None)
            log.info(f"delete neighbor for {self.ip} : {ip}")
            data['status'] = True
        writer.write(bytes(sizeof(data).encode()))
        writer.write(bytes(json.dumps(data).encode()))

    async def startUp(self, seeds: list[str]):
        nodes = []
        for seed in seeds:
            ip, port = seed.split(':')
            port = int(port)
            request = {
                "status": True,
                "uid": self.uid,
                "type": ConnectionCode.NEW_NEIGHBORS_REQUEST,
                "src_ip": self.ip,
                "dst_ip": seed,
                "number_connections_requests": TOTAL_NUMBER_CONNECTIONS,
                "p2p_nodes": [],
                "passed_nodes": [self.ip]
            }
            data = json.dumps(request)
            response = await self.connectAndSend(ip, port, data)
            log.debug(f'receive message from {ip}:{port}: {response.decode()}')
            response = json.loads(response.decode())

            nodes += response['p2p_nodes']
            if (
                response['status'] == True
            and response['number_connections_requests'] == 0
            and response['type'] == ConnectionCode.NEW_NEIGHBORS_FIND
            ):
                break

        for node in nodes:
            ip, port = node.split(":")
            request = {
                "status": True,
                "uid": self.uid,
                "type": ConnectionCode.NEW_NEIGHBOR,
                "src_ip": self.ip,
                "dst_ip": node,
                "new_node": f"{self.ip}:{self.port}"
            }
            reader, writer = await self.connectTo(ip, port)
            writer.write(bytes(sizeof(request).encode()))
            writer.write(bytes(json.dumps(request).encode()))
            self.neighbors[self.calculate_uid(ip, str(port))] = (ip, port)
            log.info(f"new neighbors for {self.ip} : {ip}")

    @staticmethod
    def calculate_uid(ip, port):
        return md5((ip + str(port)).encode()).hexdigest()