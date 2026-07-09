from __future__ import annotations

from dataclasses import dataclass, field

from backend.media_gateway.protocol import MediaPacket, PacketHeader
from deepfake_stream_signature import (
    DEFAULT_ISSUER,
    DEFAULT_KEY_ID,
    SignatureConfig,
    SignatureStatus,
    StreamPacket,
    StreamPacketHeader,
    StreamSignatureVerifier as CoreStreamSignatureVerifier,
    StreamSigner as CoreStreamSigner,
    parse_key_value_pairs,
)


@dataclass(frozen=True)
class VerificationResult:
    status: SignatureStatus
    packet: MediaPacket
    reason: str = ""
    key_id: str = ""
    manifest: dict = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.status in {
            SignatureStatus.DISABLED,
            SignatureStatus.ABSENT,
            SignatureStatus.TRUSTED,
        }


class StreamSigner:
    def __init__(self, cfg: SignatureConfig) -> None:
        self._signer = CoreStreamSigner(cfg)

    def sign_packet(self, packet: MediaPacket) -> MediaPacket:
        return _from_stream_packet(self._signer.sign_packet(_to_stream_packet(packet)), packet.header)


class StreamSignatureVerifier:
    def __init__(self, trusted_keys: dict[str, bytes] | None = None) -> None:
        self._verifier = CoreStreamSignatureVerifier(trusted_keys)

    def verify_and_strip(self, packet: MediaPacket) -> VerificationResult:
        result = self._verifier.verify_and_strip(_to_stream_packet(packet))
        return VerificationResult(
            status=result.status,
            packet=_from_stream_packet(result.packet, packet.header),
            reason=result.reason,
            key_id=result.key_id,
            manifest=result.manifest,
        )


def _to_stream_packet(packet: MediaPacket) -> StreamPacket:
    return StreamPacket(
        header=StreamPacketHeader(
            stream_type=int(packet.header.stream_type),
            codec=int(packet.header.codec),
            session_id=packet.header.session_id,
            sequence_number=packet.header.sequence_number,
            timestamp_us=packet.header.timestamp_us,
            payload_size=packet.header.payload_size,
            fragment_index=packet.header.fragment_index,
            fragment_count=packet.header.fragment_count,
        ),
        payload=packet.payload,
    )


def _from_stream_packet(packet: StreamPacket, original_header: PacketHeader) -> MediaPacket:
    return MediaPacket(
        header=PacketHeader(
            stream_type=original_header.stream_type,
            codec=original_header.codec,
            session_id=packet.header.session_id,
            sequence_number=packet.header.sequence_number,
            timestamp_us=packet.header.timestamp_us,
            payload_size=len(packet.payload),
            fragment_index=packet.header.fragment_index,
            fragment_count=packet.header.fragment_count,
        ),
        payload=packet.payload,
    )
