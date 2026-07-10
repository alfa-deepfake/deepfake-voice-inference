# deepfake-audio-video-inference

Headless realtime media inference service for the current deepfake pipeline.

The laptop captures microphone and webcam input, sends both streams through a
single SSH-tunneled TCP connection to the cluster, and receives processed audio
and video back for local preview.

The repository is intentionally trimmed down to runtime-critical pieces:

- `backend/media_gateway`: audio/video stream server, client, protocol, and
  Deep-Live-Cam adapter.
- `tools/rvc_for_realtime.py`: realtime RVC audio inference path.
- `infer/lib/infer_pack`, `infer/lib/jit`, `infer/lib/rmvpe.py`: minimal RVC
  inference internals needed by the realtime processor.
- `configs`: model/runtime configuration used by RVC.
- `assets`: model locations and placeholder files. Large model weights are not
  committed.

## Server Directory

The active cluster clone is:

```bash
/home/master/work/deepfake-audio-video-inference
```

The local laptop clone is usually:

```bash
~/work/deepfake-audio-video-inference
```

## Required Runtime Assets

The stream server expects these files on the cluster:

```text
assets/weights/voice_model.pth
assets/indices/voice_model.index
assets/hubert/hubert_base.pt
```

Deep-Live-Cam is expected at:

```text
~/workspace_w9line/deep_face/extracted/Deep-Live-Cam
```

The current source face used by the documented command is:

```text
~/workspace_w9line/deep_face/extracted/Deep-Live-Cam/классный_чел_пнг.jpg
```

## Configure Once

All launch settings (cluster address, model paths, video/signature options)
live in one file. Copy the template and edit it for your machine:

```bash
cp scripts/config.env.example scripts/config.env
$EDITOR scripts/config.env
```

`scripts/config.env` is gitignored, so the cluster address and any signature
secret stay out of git. Every value has a default; you only set what differs.

## Start Realtime Stream

The three steps below map to the three scripts in `scripts/`. Each script reads
`config.env`, activates the venv, and sets `PYTHONPATH` for you.

### 1. Start The Server On The Cluster

```bash
./scripts/server.sh
```

Expected log:

```text
media_gateway.stream_server: stream server listening on tcp://127.0.0.1:13000
```

For a persistent session, run it inside `tmux`/`screen`, or redirect it to
`/tmp/media_gateway_stream.log`.

### 2. Open The SSH Tunnel On The Laptop

In a separate laptop terminal (keep it open while streaming):

```bash
./scripts/tunnel.sh
```

### 3. Run The Laptop Client

In another laptop terminal (there is no client wrapper script — run the module
directly):

```bash
PYTHONPATH=$PWD python -m backend.media_gateway.stream_client \
  --gateway-host 127.0.0.1 --gateway-port 13000 \
  --video-width 512 --video-height 288 --video-fps 15 --jpeg-quality 65
```

Expected laptop log:

```text
media_gateway.stream_client: connected to tcp://127.0.0.1:13000
media_gateway.stream_client: microphone capture started
media_gateway.stream_client: webcam capture started
```

The preview window is named `media-gateway-stream-preview`.

The server wrapper passes extra flags straight through to the underlying module,
and `DRYRUN=1` prints the assembled command without running it:

```bash
python -m backend.media_gateway.stream_client --help
DRYRUN=1 ./scripts/server.sh
```

## Checks

Check whether the cluster server is listening:

```bash
ssh -i "$SSH_KEY" -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" "ss -ltnp | grep 13000 || true"
```

Check laptop preview dependencies:

```bash
python - <<'PY'
import tkinter
from PIL import Image, ImageTk
print("tkinter and Pillow preview dependencies are available")
PY
```

## Tuning

Pass client flags straight through for a one-off run:

```bash
# Lower bandwidth and latency
python -m backend.media_gateway.stream_client --video-fps 12 --jpeg-quality 55

# Better visual quality
python -m backend.media_gateway.stream_client \
  --video-width 640 --video-height 360 --jpeg-quality 75
```

For a permanent server-side change, edit the matching value in `config.env`.

## Signed Streams

To run a C2PA-like signed stream, set the server-side signature values in
`config.env`:

```bash
SIGNATURE_POLICY="log"          # off | log | block  (server-side)
SIGNATURE_KEY="dev-secret"      # becomes the trusted verifier key
```

`server.sh` reads these and verifies automatically. The client runs as a bare
module, so pass its signing flags directly:

```bash
python -m backend.media_gateway.stream_client ... \
  --signature-key dev-secret --signature-key-id deepfake-client-test
```

See `docs/en/media_gateway.md` for what each policy does.

## License

This project keeps the upstream RVC MIT license. See `LICENSE`.
