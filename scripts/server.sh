#!/usr/bin/env bash
# Start the stream inference server. Run this ON THE CLUSTER.
#
#   ./scripts/server.sh              # normal launch
#   DRYRUN=1 ./scripts/server.sh     # print the command instead of running it
#
# Extra flags are passed straight through, e.g. ./scripts/server.sh --help
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
activate_venv

signature_verifier_flags SIGFLAGS

run python -m backend.media_gateway.stream_server \
  --host "$STREAM_HOST" \
  --port "$STREAM_PORT" \
  --audio-model-path "$AUDIO_MODEL_PATH" \
  --audio-index-path "$AUDIO_INDEX_PATH" \
  --audio-index-rate "$AUDIO_INDEX_RATE" \
  --audio-sample-rate "$AUDIO_SAMPLE_RATE" \
  --audio-block-time "$AUDIO_BLOCK_TIME" \
  --audio-f0method "$AUDIO_F0METHOD" \
  --video-dlc-root "$VIDEO_DLC_ROOT" \
  --video-source-face "$VIDEO_SOURCE_FACE" \
  --video-python-path "$VIDEO_PYTHON_PATH" \
  --video-cuda-lib-root "$VIDEO_CUDA_LIB_ROOT" \
  --video-execution-provider "$VIDEO_EXECUTION_PROVIDER" \
  --video-camera-fps "$VIDEO_CAMERA_FPS" \
  "${SIGFLAGS[@]}" \
  "$@"
