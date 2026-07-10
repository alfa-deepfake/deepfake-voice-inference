# Media Gateway

The media gateway is the backend layer above the voice and face engines. It
accepts live microphone and webcam streams over a single SSH-tunneled TCP
connection, routes audio into the RVC engine, routes video into the
Deep-Live-Cam adapter, and returns the processed streams to the client for
local preview.

## Component Layout

```text
stream_client (laptop)
  -> capture microphone + webcam
  -> packetize + optionally sign
  -> one length-prefixed TCP stream over the SSH tunnel
  -> local audio playback + preview window

stream_server (cluster)
  -> audio inference engine (RVC)
  -> video inference engine (Deep-Live-Cam adapter, subprocess)
  -> packet reassembly / fragmentation
  -> processed output packets back down the same connection
```

## Transport

The wire protocol and length-prefixed framing live in the standalone sibling
package `deepfake-media-transport` (`deepfake_media_transport`), so the format
is shared with `deepfake-virtualcam-check` instead of being duplicated.

Packet header (`deepfake_media_transport.protocol`):

- 2 bytes magic (`DF`)
- 1 byte version
- 1 byte stream type (audio / video / control)
- 16 bytes session id
- 8 bytes sequence number
- 8 bytes timestamp in microseconds
- 2 bytes codec id
- 4 bytes payload size
- 2 bytes fragment index
- 2 bytes fragment count

This keeps the transport stateful enough for packet-loss detection,
audio/video synchronization, multiple concurrent sessions, and large video
frame fragmentation. On the wire each packet is carried as a length-prefixed
TCP frame (`deepfake_media_transport.framing`).

## Signatures

Signing and verification are provided by the sibling package
`deepfake-stream-signature`. `stream_signature.py` adapts its transport-agnostic
`StreamPacket` API to the gateway's `MediaPacket`. See `--signature-policy`
below.

## Stream Mode

Start the stream server on the cluster:

```bash
TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 PYTHONPATH=$PWD .venv/bin/python -m backend.media_gateway.stream_server \
  --host 127.0.0.1 \
  --port 13000 \
  --audio-model-path assets/weights/voice_model.pth \
  --audio-index-path assets/indices/voice_model.index \
  --audio-index-rate 0.3 \
  --video-dlc-root ~/workspace_w9line/deep_face/extracted/Deep-Live-Cam \
  --video-source-face ~/workspace_w9line/deep_face/extracted/Deep-Live-Cam/классный_чел_пнг.jpg \
  --video-python-path ~/workspace_w9line/deep_face/extracted/Deep-Live-Cam/.venv_dlc/bin/python \
  --video-cuda-lib-root ~/work/deepfake-audio-video-inference/.venv/lib/python3.10/site-packages \
  --video-execution-provider cuda \
  --video-camera-fps 15.0
```

Create the SSH tunnel on the operator machine:

```bash
ssh -i /tmp/deepfake_voice_cluster_key -p 22010 \
  -N -L 13000:127.0.0.1:13000 master@62.183.4.208
```

Run the combined stream client on the operator machine:

```bash
PYTHONPATH=$PWD python -m backend.media_gateway.stream_client \
  --gateway-host 127.0.0.1 \
  --gateway-port 13000 \
  --video-width 512 \
  --video-height 288 \
  --video-fps 15 \
  --jpeg-quality 65
```

The `scripts/server.sh`, `scripts/tunnel.sh`, and `scripts/client.sh` wrappers
read `scripts/config.env` and assemble these commands for you.

## Signed Streams

To simulate a C2PA-like signed stream, start the server with signature checking
and pass the same test key to the client:

```bash
TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 PYTHONPATH=$PWD .venv/bin/python -m backend.media_gateway.stream_server \
  ... \
  --signature-policy log \
  --signature-trusted-key deepfake-client-test=dev-secret
```

```bash
PYTHONPATH=$PWD python -m backend.media_gateway.stream_client \
  ... \
  --signature-key dev-secret \
  --signature-key-id deepfake-client-test
```

- `--signature-policy off` disables verification.
- `log` verifies and strips signature envelopes before inference while warning
  on untrusted, tampered, or replayed signatures.
- `block` drops packets with invalid signature envelopes.

Unsigned packets are still accepted, so an unsigned client keeps working.

## Not Implemented Yet

- jitter buffer logic
- A/V synchronization policy
- robust reconnect / session teardown
