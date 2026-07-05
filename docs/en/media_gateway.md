# Media Gateway Plan

This module is the next backend layer above the existing voice and face
engines. Its purpose is to accept live microphone and webcam streams over UDP,
route audio into `deepfake-voice-inference`, route video into Deep-Live-Cam,
and return processed streams to a preview client.

## Component Layout

```text
capture_client
  -> audio UDP packets
  -> video UDP packets

media_gateway
  -> audio inference engine (RVC)
  -> video inference engine (Deep-Live-Cam adapter)
  -> UDP packet reassembly / fragmentation
  -> output packets

preview_client
  -> audio playback
  -> video window
  -> latency/fps overlay
```

## First Port Plan

- `11000/udp`: gateway input
- `11001/udp`: audio output
- `11002/udp`: video output

The current implementation processes audio packets and returns them to
`client_port + 1`. Video packets are processed through a separate
Deep-Live-Cam worker process and returned to `client_port + 2`.

The preview client registers its own UDP return ports with the gateway by
sending control packets from the exact sockets that will receive audio and
video. This makes the return path work through NAT, as long as the gateway can
reply to the same public UDP mappings created by the preview client.

## Protocol

`backend/media_gateway/protocol.py` defines a binary packet header:

- 2 bytes magic
- 1 byte version
- 1 byte stream type
- 16 bytes session id
- 8 bytes sequence number
- 8 bytes timestamp in microseconds
- 2 bytes codec id
- 4 bytes payload size
- 2 bytes fragment index
- 2 bytes fragment count

This keeps the transport stateful enough for:

- packet loss detection
- audio/video synchronization
- multiple concurrent sessions
- large video frame fragmentation over UDP

## Current Status

Implemented:

- packet header and codec enums
- session bookkeeping
- audio engine adapter using the existing realtime RVC processor
- UDP gateway server
- video engine adapter that delegates frame processing to a dedicated
  Deep-Live-Cam Python environment
- `capture_client.py` for webcam + microphone packetization
- `preview_client.py` for audio/video preview
- UDP fragmentation and reassembly for large MJPEG frames

Not implemented yet:

- jitter buffer logic
- A/V synchronization policy
- robust reconnect / session teardown

## Recommended Next Steps

1. Implement a minimal jitter buffer for audio and video.
2. Add session metrics and overlay telemetry.
3. Add reconnect logic and graceful shutdown.
4. Add optional recording of input/output streams for debugging.
5. Decide whether long-term transport should stay raw UDP or move to RTP/WebRTC.

## First End-to-End Loop

If the provider exposes UDP to the gateway, use the UDP flow below. If external
UDP is blocked, use the UDP-over-SSH bridge instead.

## UDP over SSH

Start the server-side bridge on the cluster. It listens on TCP localhost and
forwards tunneled datagrams to the UDP gateway on `127.0.0.1:12000`:

```bash
PYTHONPATH=$PWD .venv/bin/python -m backend.media_gateway.udp_tcp_tunnel server \
  --tcp-host 127.0.0.1 \
  --tcp-port 13000 \
  --udp-host 127.0.0.1 \
  --udp-port 12000
```

Create the SSH tunnel on the operator machine. This is a TCP tunnel used only
to carry UDP datagrams between the two Python bridge processes:

```bash
ssh -i /tmp/deepfake_voice_cluster_key -p 22010 \
  -N -L 13000:127.0.0.1:13000 master@62.183.4.208
```

Run the local bridge on the operator machine. It exposes a local UDP gateway at
`127.0.0.1:12000`:

```bash
PYTHONPATH=$PWD python -m backend.media_gateway.udp_tcp_tunnel client \
  --udp-host 127.0.0.1 \
  --udp-port 12000 \
  --tcp-host 127.0.0.1 \
  --tcp-port 13000
```

Then run the regular UDP preview and capture clients against the local bridge:

```bash
PYTHONPATH=$PWD python -m backend.media_gateway.preview_client \
  --gateway-host 127.0.0.1 \
  --gateway-port 12000 \
  --audio-port 11001 \
  --video-port 11002
```

```bash
PYTHONPATH=$PWD python -m backend.media_gateway.capture_client \
  --gateway-host 127.0.0.1 \
  --gateway-port 12000
```

## UDP Mode

Start the gateway on the cluster:

```bash
PYTHONPATH=$PWD .venv/bin/python -m backend.media_gateway.server \
  --port 12000 \
  --audio-model-path assets/weights/voice_model.pth \
  --audio-index-path assets/indices/voice_model.index \
  --audio-index-rate 0.3 \
  --video-dlc-root ~/workspace_w9line/deep_face/extracted/Deep-Live-Cam \
  --video-source-face ~/workspace_w9line/deep_face/extracted/Deep-Live-Cam/классный_чел_пнг.jpg \
  --video-python-path ~/workspace_w9line/deep_face/extracted/Deep-Live-Cam/.venv_dlc/bin/python \
  --video-cuda-lib-root ~/work/deepfake-voice-inference/.venv/lib/python3.10/site-packages \
  --video-execution-provider cuda \
  --video-camera-fps 20.0
```

Run capture on the operator machine:

```bash
python -m backend.media_gateway.capture_client \
  --gateway-host CLUSTER_IP \
  --gateway-port 12000
```

Run preview on the operator machine:

```bash
python -m backend.media_gateway.preview_client \
  --gateway-host CLUSTER_IP \
  --gateway-port 12000 \
  --audio-port 11001 \
  --video-port 11002
```
