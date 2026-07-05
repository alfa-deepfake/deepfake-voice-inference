from __future__ import annotations

import argparse
import logging
import socket
import struct
import threading
from dataclasses import dataclass


LENGTH_STRUCT = struct.Struct("!I")
MAX_DATAGRAM_SIZE = 2 * 1024 * 1024
logger = logging.getLogger("media_gateway.udp_tcp_tunnel")


def read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def write_frame(sock: socket.socket, payload: bytes) -> None:
    if len(payload) > MAX_DATAGRAM_SIZE:
        raise ValueError(f"datagram too large for tunnel: {len(payload)}")
    sock.sendall(LENGTH_STRUCT.pack(len(payload)) + payload)


def read_frame(sock: socket.socket) -> bytes:
    length = LENGTH_STRUCT.unpack(read_exact(sock, LENGTH_STRUCT.size))[0]
    if length > MAX_DATAGRAM_SIZE:
        raise ValueError(f"frame too large for tunnel: {length}")
    return read_exact(sock, length)


@dataclass(frozen=True)
class ServerConfig:
    tcp_host: str
    tcp_port: int
    udp_host: str
    udp_port: int


@dataclass(frozen=True)
class ClientConfig:
    udp_host: str
    udp_port: int
    tcp_host: str
    tcp_port: int


def run_server(cfg: ServerConfig) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((cfg.tcp_host, cfg.tcp_port))
    server.listen()
    logger.info(
        "server bridge listening tcp://%s:%s -> udp://%s:%s",
        cfg.tcp_host,
        cfg.tcp_port,
        cfg.udp_host,
        cfg.udp_port,
    )
    while True:
        tcp_sock, addr = server.accept()
        logger.info("accepted tunnel connection from %s", addr)
        threading.Thread(target=handle_server_connection, args=(cfg, tcp_sock, addr), daemon=True).start()


def handle_server_connection(cfg: ServerConfig, tcp_sock: socket.socket, addr) -> None:
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.connect((cfg.udp_host, cfg.udp_port))

    def udp_to_tcp() -> None:
        while True:
            payload = udp_sock.recv(MAX_DATAGRAM_SIZE)
            write_frame(tcp_sock, payload)

    threading.Thread(target=udp_to_tcp, daemon=True).start()
    try:
        while True:
            udp_sock.send(read_frame(tcp_sock))
    except Exception as exc:
        logger.info("closed tunnel connection from %s: %s", addr, exc)
    finally:
        tcp_sock.close()
        udp_sock.close()


def run_client(cfg: ClientConfig) -> None:
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_sock.bind((cfg.udp_host, cfg.udp_port))
    clients: dict[tuple[str, int], socket.socket] = {}
    lock = threading.Lock()
    logger.info(
        "client bridge listening udp://%s:%s -> tcp://%s:%s",
        cfg.udp_host,
        cfg.udp_port,
        cfg.tcp_host,
        cfg.tcp_port,
    )
    while True:
        payload, addr = udp_sock.recvfrom(MAX_DATAGRAM_SIZE)
        with lock:
            tcp_sock = clients.get(addr)
            if tcp_sock is None:
                tcp_sock = socket.create_connection((cfg.tcp_host, cfg.tcp_port))
                clients[addr] = tcp_sock
                threading.Thread(target=client_tcp_to_udp, args=(udp_sock, tcp_sock, addr, clients, lock), daemon=True).start()
                logger.info("opened tunnel connection for udp client %s", addr)
        try:
            write_frame(tcp_sock, payload)
        except Exception:
            with lock:
                clients.pop(addr, None)
            tcp_sock.close()


def client_tcp_to_udp(
    udp_sock: socket.socket,
    tcp_sock: socket.socket,
    addr: tuple[str, int],
    clients: dict[tuple[str, int], socket.socket],
    lock: threading.Lock,
) -> None:
    try:
        while True:
            udp_sock.sendto(read_frame(tcp_sock), addr)
    except Exception as exc:
        logger.info("closed tunnel connection for udp client %s: %s", addr, exc)
    finally:
        with lock:
            clients.pop(addr, None)
        tcp_sock.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Bridge UDP datagrams through a length-framed TCP tunnel.")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    server = subparsers.add_parser("server")
    server.add_argument("--tcp-host", default="127.0.0.1")
    server.add_argument("--tcp-port", type=int, default=13000)
    server.add_argument("--udp-host", default="127.0.0.1")
    server.add_argument("--udp-port", type=int, default=12000)

    client = subparsers.add_parser("client")
    client.add_argument("--udp-host", default="127.0.0.1")
    client.add_argument("--udp-port", type=int, default=12000)
    client.add_argument("--tcp-host", default="127.0.0.1")
    client.add_argument("--tcp-port", type=int, default=13000)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    if args.mode == "server":
        run_server(ServerConfig(args.tcp_host, args.tcp_port, args.udp_host, args.udp_port))
    else:
        run_client(ClientConfig(args.udp_host, args.udp_port, args.tcp_host, args.tcp_port))


if __name__ == "__main__":
    main()
