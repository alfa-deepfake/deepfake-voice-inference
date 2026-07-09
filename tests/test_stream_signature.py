from __future__ import annotations

import unittest

from backend.media_gateway.protocol import Codec, MediaPacket, PacketHeader, StreamType, packetize_payload
from backend.media_gateway.stream_signature import (
    SignatureConfig,
    SignatureStatus,
    StreamSignatureVerifier,
    StreamSigner,
)


class StreamSignatureTest(unittest.TestCase):
    def test_sign_and_verify_packet(self) -> None:
        signer = StreamSigner(SignatureConfig(enabled=True, key_id="test", secret=b"secret"))
        verifier = StreamSignatureVerifier({"test": b"secret"})

        packet = signer.sign_packet(media_packet(sequence_number=1, payload=b"audio"))
        result = verifier.verify_and_strip(packet)

        self.assertEqual(SignatureStatus.TRUSTED, result.status)
        self.assertEqual(b"audio", result.packet.payload)

    def test_detects_tampered_payload(self) -> None:
        signer = StreamSigner(SignatureConfig(enabled=True, key_id="test", secret=b"secret"))
        verifier = StreamSignatureVerifier({"test": b"secret"})

        packet = signer.sign_packet(media_packet(sequence_number=1, payload=b"audio"))
        tampered = MediaPacket(packet.header, packet.payload + b"x")
        result = verifier.verify_and_strip(tampered)

        self.assertEqual(SignatureStatus.TAMPERED, result.status)

    def test_detects_untrusted_key(self) -> None:
        signer = StreamSigner(SignatureConfig(enabled=True, key_id="attacker", secret=b"secret"))
        verifier = StreamSignatureVerifier({"trusted": b"secret"})

        result = verifier.verify_and_strip(signer.sign_packet(media_packet(sequence_number=1)))

        self.assertEqual(SignatureStatus.UNTRUSTED_KEY, result.status)

    def test_detects_replay(self) -> None:
        signer = StreamSigner(SignatureConfig(enabled=True, key_id="test", secret=b"secret"))
        verifier = StreamSignatureVerifier({"test": b"secret"})

        packet = signer.sign_packet(media_packet(sequence_number=1))
        self.assertEqual(SignatureStatus.TRUSTED, verifier.verify_and_strip(packet).status)

        result = verifier.verify_and_strip(packet)

        self.assertEqual(SignatureStatus.CHAIN_MISMATCH, result.status)

    def test_signed_payload_can_be_fragmented_and_reassembled(self) -> None:
        signer = StreamSigner(SignatureConfig(enabled=True, key_id="test", secret=b"secret"))
        verifier = StreamSignatureVerifier({"test": b"secret"})
        signed = signer.sign_packet(media_packet(sequence_number=1, payload=b"x" * 100))

        fragments = packetize_payload(
            stream_type=signed.header.stream_type,
            codec=signed.header.codec,
            session_id=signed.header.session_id,
            sequence_number=signed.header.sequence_number,
            timestamp_us=signed.header.timestamp_us,
            payload=signed.payload,
            max_payload_size=40,
        )
        reassembled_payload = b"".join(fragment.payload for fragment in fragments)
        reassembled = media_packet(sequence_number=1, payload=reassembled_payload)

        result = verifier.verify_and_strip(reassembled)

        self.assertEqual(SignatureStatus.TRUSTED, result.status)
        self.assertEqual(b"x" * 100, result.packet.payload)


def media_packet(sequence_number: int = 1, payload: bytes = b"payload") -> MediaPacket:
    session_id = b"test-session".ljust(16, b"\x00")
    return MediaPacket(
        header=PacketHeader(
            stream_type=StreamType.AUDIO,
            codec=Codec.PCM16,
            session_id=session_id,
            sequence_number=sequence_number,
            timestamp_us=123456,
            payload_size=len(payload),
        ),
        payload=payload,
    )


if __name__ == "__main__":
    unittest.main()
