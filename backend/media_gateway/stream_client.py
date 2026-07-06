from __future__ import annotations

import argparse
import json
import logging
import queue
import socket
import struct
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from typing import Optional

import numpy as np

from backend.media_gateway.protocol import Codec, MediaPacket, PacketReassembler, StreamType, packetize_payload


LENGTH_STRUCT = struct.Struct("!I")
MAX_FRAME_SIZE = 16 * 1024 * 1024
DEFAULT_SESSION_ID = "default"
logger = logging.getLogger("media_gateway.stream_client")


try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    cv2 = None

try:
    import sounddevice as sd  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    sd = None


@dataclass(frozen=True)
class StreamClientConfig:
    gateway_host: str
    gateway_port: int
    session_id: bytes
    audio_sample_rate: int
    audio_block_samples: int
    audio_device: Optional[int]
    video_device: int
    video_width: int
    video_height: int
    video_fps: float
    jpeg_quality: int
    source_wav: Optional[str]


def parse_args() -> StreamClientConfig:
    parser = argparse.ArgumentParser(description="Low-latency capture and preview client for the stream media gateway.")
    parser.add_argument("--gateway-host", default="127.0.0.1")
    parser.add_argument("--gateway-port", type=int, default=13000)
    parser.add_argument("--session-id", default=DEFAULT_SESSION_ID)
    parser.add_argument("--audio-sample-rate", type=int, default=48000)
    parser.add_argument("--audio-block-samples", type=int, default=12000)
    parser.add_argument("--audio-device", type=int, default=None)
    parser.add_argument("--video-device", type=int, default=0)
    parser.add_argument("--video-width", type=int, default=512)
    parser.add_argument("--video-height", type=int, default=288)
    parser.add_argument("--video-fps", type=float, default=15.0)
    parser.add_argument("--jpeg-quality", type=int, default=65)
    parser.add_argument("--source-wav", default=None, help="Read audio from wav file instead of microphone")
    args = parser.parse_args()
    session_id = (
        args.session_id.encode("utf-8")[:16].ljust(16, b"\x00")
        if args.session_id
        else uuid.uuid4().bytes
    )
    return StreamClientConfig(
        gateway_host=args.gateway_host,
        gateway_port=args.gateway_port,
        session_id=session_id,
        audio_sample_rate=args.audio_sample_rate,
        audio_block_samples=args.audio_block_samples,
        audio_device=args.audio_device,
        video_device=args.video_device,
        video_width=args.video_width,
        video_height=args.video_height,
        video_fps=args.video_fps,
        jpeg_quality=args.jpeg_quality,
        source_wav=args.source_wav,
    )


class StreamConnection:
    def __init__(self, cfg: StreamClientConfig) -> None:
        self.cfg = cfg
        self.sock = socket.create_connection((cfg.gateway_host, cfg.gateway_port), timeout=15)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.send_lock = threading.Lock()
        self.reassembler = PacketReassembler()
        self.audio_sequence = 0
        self.video_sequence = 0
        self.control_sequence = 0
        logger.info("connected to tcp://%s:%s", cfg.gateway_host, cfg.gateway_port)

    def send_control(self, payload: dict) -> None:
        self._send_payload(StreamType.CONTROL, Codec.JSON, json.dumps(payload).encode("utf-8"), self.control_sequence)
        self.control_sequence += 1

    def send_audio(self, payload: bytes) -> None:
        self._send_payload(StreamType.AUDIO, Codec.PCM16, payload, self.audio_sequence)
        self.audio_sequence += 1

    def send_video(self, payload: bytes) -> None:
        self._send_payload(StreamType.VIDEO, Codec.MJPEG, payload, self.video_sequence)
        self.video_sequence += 1

    def receive(self) -> MediaPacket:
        while True:
            length = LENGTH_STRUCT.unpack(read_exact(self.sock, LENGTH_STRUCT.size))[0]
            if length > MAX_FRAME_SIZE:
                raise ValueError(f"stream frame too large: {length}")
            packet = self.reassembler.push(MediaPacket.from_bytes(read_exact(self.sock, length)))
            if packet is not None:
                return packet

    def _send_payload(self, stream_type: StreamType, codec: Codec, payload: bytes, sequence_number: int) -> None:
        packets = packetize_payload(
            stream_type=stream_type,
            codec=codec,
            session_id=self.cfg.session_id,
            sequence_number=sequence_number,
            timestamp_us=time.time_ns() // 1000,
            payload=payload,
        )
        with self.send_lock:
            for packet in packets:
                data = packet.to_bytes()
                self.sock.sendall(LENGTH_STRUCT.pack(len(data)) + data)


def response_loop(conn: StreamConnection, audio_queue: queue.Queue, video_queue: queue.Queue) -> None:
    while True:
        packet = conn.receive()
        if packet.header.stream_type == StreamType.AUDIO and packet.header.codec == Codec.PCM16:
            put_latest(audio_queue, bytes(packet.payload))
        elif packet.header.stream_type == StreamType.VIDEO:
            put_latest(video_queue, bytes(packet.payload))


def audio_sender(audio_queue: queue.Queue, conn: StreamConnection) -> None:
    while True:
        conn.send_audio(audio_queue.get())


def video_sender(video_queue: queue.Queue, conn: StreamConnection) -> None:
    while True:
        conn.send_video(video_queue.get())


def microphone_loop(cfg: StreamClientConfig, audio_queue: queue.Queue) -> None:
    if cfg.source_wav:
        wav_loop(cfg, audio_queue)
        return
    if sd is None:
        raise RuntimeError("sounddevice is required for microphone capture")

    def callback(indata, frames, time_info, status) -> None:
        del frames, time_info
        if status:
            logger.warning("audio input status: %s", status)
        pcm = np.clip(indata[:, 0], -1.0, 1.0)
        put_latest(audio_queue, (pcm * 32767.0).astype("<i2", copy=False).tobytes())

    with sd.InputStream(
        samplerate=cfg.audio_sample_rate,
        blocksize=cfg.audio_block_samples,
        device=cfg.audio_device,
        channels=1,
        dtype="float32",
        callback=callback,
    ):
        logger.info("microphone capture started")
        while True:
            time.sleep(1)


def wav_loop(cfg: StreamClientConfig, audio_queue: queue.Queue) -> None:
    with wave.open(cfg.source_wav, "rb") as wav_file:
        if wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2:
            raise ValueError("source wav must be mono 16-bit PCM")
        if wav_file.getframerate() != cfg.audio_sample_rate:
            raise ValueError("source wav sample rate must match audio-sample-rate")
        block_bytes = cfg.audio_block_samples * 2
        while True:
            chunk = wav_file.readframes(cfg.audio_block_samples)
            if not chunk:
                wav_file.rewind()
                continue
            if len(chunk) < block_bytes:
                chunk += b"\x00" * (block_bytes - len(chunk))
            put_latest(audio_queue, chunk)
            time.sleep(cfg.audio_block_samples / cfg.audio_sample_rate)


def webcam_loop(cfg: StreamClientConfig, video_queue: queue.Queue) -> None:
    if cv2 is None:
        raise RuntimeError("opencv-python is required for webcam capture")
    cap = cv2.VideoCapture(cfg.video_device)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.video_width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.video_height)
    cap.set(cv2.CAP_PROP_FPS, cfg.video_fps)
    if not cap.isOpened():
        raise RuntimeError(f"failed to open webcam device {cfg.video_device}")

    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), cfg.jpeg_quality]
    frame_interval = 1.0 / cfg.video_fps
    logger.info("webcam capture started")
    try:
        while True:
            started = time.perf_counter()
            ok, frame = cap.read()
            if not ok:
                logger.warning("failed to read webcam frame")
                continue
            ok, encoded = cv2.imencode(".jpg", frame, encode_params)
            if ok:
                put_latest(video_queue, encoded.tobytes())
            elapsed = time.perf_counter() - started
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)
    finally:
        cap.release()


def audio_playback(audio_queue: queue.Queue, sample_rate: int, block_samples: int) -> None:
    if sd is None:
        logger.warning("sounddevice not installed; audio preview disabled")
        while True:
            audio_queue.get()

    def callback(outdata, frames, time_info, status) -> None:
        del frames, time_info
        if status:
            logger.warning("audio output status: %s", status)
        try:
            payload = payload_as_bytes(audio_queue.get_nowait())
            if payload is None:
                logger.warning("dropping non-bytes audio payload")
                outdata.fill(0)
                return
            pcm = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
            outdata[:, 0] = pcm[: outdata.shape[0]]
        except queue.Empty:
            outdata.fill(0)

    with sd.OutputStream(
        samplerate=sample_rate,
        blocksize=block_samples,
        channels=1,
        dtype="float32",
        callback=callback,
    ):
        while True:
            time.sleep(1)


def video_preview(video_queue: queue.Queue) -> None:
    if cv2 is None:
        logger.warning("opencv-python not installed; video preview disabled")
        while True:
            video_queue.get()

    while True:
        payload = payload_as_bytes(video_queue.get())
        if payload is None:
            logger.warning("dropping non-bytes video payload")
            continue
        try:
            frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        except cv2.error as exc:
            logger.warning("failed to decode video frame bytes=%s: %s", len(payload), exc)
            continue
        if frame is None:
            logger.warning("failed to decode video frame bytes=%s", len(payload))
            continue
        cv2.imshow("media-gateway-stream-preview", frame)
        if cv2.waitKey(1) & 0xFF == 27:
            break


def payload_as_bytes(payload: object) -> Optional[bytes]:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray):
        return bytes(payload)
    if isinstance(payload, memoryview):
        return payload.tobytes()
    if isinstance(payload, np.ndarray):
        return payload.tobytes()
    return None


def put_latest(item_queue: queue.Queue, item: bytes) -> None:
    try:
        item_queue.put_nowait(item)
    except queue.Full:
        try:
            item_queue.get_nowait()
        except queue.Empty:
            pass
        item_queue.put_nowait(item)


def read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("stream connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = parse_args()
    conn = StreamConnection(cfg)
    conn.send_control(
        {
            "kind": "stream_client_started",
            "audio_sample_rate": cfg.audio_sample_rate,
            "audio_block_samples": cfg.audio_block_samples,
            "video_fps": cfg.video_fps,
            "video_width": cfg.video_width,
            "video_height": cfg.video_height,
            "jpeg_quality": cfg.jpeg_quality,
        }
    )

    outgoing_audio: queue.Queue[bytes] = queue.Queue(maxsize=4)
    outgoing_video: queue.Queue[bytes] = queue.Queue(maxsize=1)
    incoming_audio: queue.Queue[bytes] = queue.Queue(maxsize=4)
    incoming_video: queue.Queue[bytes] = queue.Queue(maxsize=1)

    threads = [
        threading.Thread(target=response_loop, args=(conn, incoming_audio, incoming_video), daemon=True),
        threading.Thread(target=audio_sender, args=(outgoing_audio, conn), daemon=True),
        threading.Thread(target=video_sender, args=(outgoing_video, conn), daemon=True),
        threading.Thread(target=microphone_loop, args=(cfg, outgoing_audio), daemon=True),
        threading.Thread(target=webcam_loop, args=(cfg, outgoing_video), daemon=True),
        threading.Thread(target=audio_playback, args=(incoming_audio, cfg.audio_sample_rate, cfg.audio_block_samples), daemon=True),
    ]
    for thread in threads:
        thread.start()
    video_preview(incoming_video)


if __name__ == "__main__":
    main()
