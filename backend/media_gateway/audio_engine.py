from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger("media_gateway.audio_engine")


@dataclass(frozen=True)
class AudioEngineConfig:
    model_path: str
    index_path: str = ""
    sample_rate: int = 48000
    block_time: float = 0.25
    crossfade_time: float = 0.05
    extra_time: float = 2.5
    pitch: int = 0
    index_rate: float = 0.0
    rms_mix_rate: float = 0.0
    f0method: str = "fcpe"
    n_cpu: int = 1
    threshold: int = -60
    device: Optional[str] = None
    is_half: Optional[bool] = None
    use_jit: bool = False


class AudioInferenceEngine:
    """Stateful realtime RVC processor for PCM16 audio blocks."""

    def __init__(self, cfg: AudioEngineConfig) -> None:
        _load_runtime_dependencies()
        self.cfg = cfg
        self.config = _build_runtime_config(cfg)
        self.device = torch.device(self.config.device)
        logger.info(
            "audio inference device=%s cuda_available=%s is_half=%s",
            self.device,
            torch.cuda.is_available(),
            self.config.is_half,
        )
        self.rvc = RVC(
            cfg.pitch,
            cfg.model_path,
            cfg.index_path,
            cfg.index_rate,
            cfg.n_cpu,
            None,
            None,
            self.config,
        )

        self.samplerate = cfg.sample_rate or self.rvc.tgt_sr
        self.zc = self.samplerate // 100
        self.block_frame = int(round(cfg.block_time * self.samplerate / self.zc)) * self.zc
        self.block_frame_16k = 160 * self.block_frame // self.zc
        self.crossfade_frame = int(round(cfg.crossfade_time * self.samplerate / self.zc)) * self.zc
        self.sola_buffer_frame = min(self.crossfade_frame, 4 * self.zc)
        self.sola_search_frame = self.zc
        self.extra_frame = int(round(cfg.extra_time * self.samplerate / self.zc)) * self.zc
        self.skip_head = self.extra_frame // self.zc
        self.return_length = (
            self.block_frame + self.sola_buffer_frame + self.sola_search_frame
        ) // self.zc

        self.input_wav = torch.zeros(
            self.extra_frame + self.crossfade_frame + self.sola_search_frame + self.block_frame,
            device=self.device,
            dtype=torch.float32,
        )
        self.input_wav_res = torch.zeros(
            160 * self.input_wav.shape[0] // self.zc,
            device=self.device,
            dtype=torch.float32,
        )
        self.sola_buffer = torch.zeros(
            self.sola_buffer_frame,
            device=self.device,
            dtype=torch.float32,
        )
        self.rms_buffer = np.zeros(4 * self.zc, dtype=np.float32)

        self.fade_in_window = (
            torch.sin(
                0.5
                * np.pi
                * torch.linspace(
                    0.0,
                    1.0,
                    steps=self.sola_buffer_frame,
                    device=self.device,
                    dtype=torch.float32,
                )
            )
            ** 2
        )
        self.fade_out_window = 1 - self.fade_in_window

        self.resampler_to_16k = tat.Resample(
            orig_freq=self.samplerate,
            new_freq=16000,
            dtype=torch.float32,
        ).to(self.device)
        self.resampler_from_model = (
            tat.Resample(
                orig_freq=self.rvc.tgt_sr,
                new_freq=self.samplerate,
                dtype=torch.float32,
            ).to(self.device)
            if self.rvc.tgt_sr != self.samplerate
            else None
        )

    def process_pcm16(self, payload: bytes) -> bytes:
        pcm = np.frombuffer(payload, dtype="<i2")
        if pcm.size == 0:
            return b""
        audio = self._fit_block(pcm.astype(np.float32) / 32768.0)
        output = np.clip(self.process_float32(audio), -1.0, 1.0)
        return (output * 32767.0).astype("<i2", copy=False).tobytes()

    def process_float32(self, input_audio: np.ndarray) -> np.ndarray:
        started = time.perf_counter()
        input_audio = self._gate_by_threshold(np.asarray(input_audio, dtype=np.float32))

        self.input_wav[:-self.block_frame] = self.input_wav[self.block_frame :].clone()
        self.input_wav[-input_audio.shape[0] :] = torch.from_numpy(input_audio).to(self.device)

        self.input_wav_res[:-self.block_frame_16k] = self.input_wav_res[
            self.block_frame_16k :
        ].clone()
        self.input_wav_res[-160 * (input_audio.shape[0] // self.zc + 1) :] = self.resampler_to_16k(
            self.input_wav[-input_audio.shape[0] - 2 * self.zc :]
        )[160:]

        output = self.rvc.infer(
            self.input_wav_res,
            self.block_frame_16k,
            self.skip_head,
            self.return_length,
            self.cfg.f0method,
        )
        if self.resampler_from_model is not None:
            output = self.resampler_from_model(output)

        output = self._mix_rms(output)
        output = self._apply_sola(output)
        logger.debug("processed block in %.4fs", time.perf_counter() - started)
        return output[: self.block_frame].detach().cpu().numpy()

    def _fit_block(self, audio: np.ndarray) -> np.ndarray:
        if audio.shape[0] == self.block_frame:
            return audio
        if audio.shape[0] > self.block_frame:
            return audio[-self.block_frame :]
        return np.pad(audio, (self.block_frame - audio.shape[0], 0))

    def _gate_by_threshold(self, input_audio: np.ndarray) -> np.ndarray:
        if self.cfg.threshold <= -60:
            return input_audio
        gated = np.append(self.rms_buffer, input_audio)
        rms = librosa.feature.rms(
            y=gated,
            frame_length=4 * self.zc,
            hop_length=self.zc,
        )[:, 2:]
        self.rms_buffer[:] = gated[-4 * self.zc :]
        gated = gated[2 * self.zc - self.zc // 2 :]
        below_threshold = librosa.amplitude_to_db(rms, ref=1.0)[0] < self.cfg.threshold
        for index, muted in enumerate(below_threshold):
            if muted:
                gated[index * self.zc : (index + 1) * self.zc] = 0
        return gated[self.zc // 2 :].astype(np.float32, copy=False)

    def _mix_rms(self, output: torch.Tensor) -> torch.Tensor:
        if self.cfg.rms_mix_rate >= 1:
            return output
        input_wav = self.input_wav[self.extra_frame :]
        input_rms = librosa.feature.rms(
            y=input_wav[: output.shape[0]].detach().cpu().numpy(),
            frame_length=4 * self.zc,
            hop_length=self.zc,
        )
        input_rms = torch.from_numpy(input_rms).to(self.device)
        input_rms = F.interpolate(
            input_rms.unsqueeze(0),
            size=output.shape[0] + 1,
            mode="linear",
            align_corners=True,
        )[0, 0, :-1]
        output_rms = librosa.feature.rms(
            y=output.detach().cpu().numpy(),
            frame_length=4 * self.zc,
            hop_length=self.zc,
        )
        output_rms = torch.from_numpy(output_rms).to(self.device)
        output_rms = F.interpolate(
            output_rms.unsqueeze(0),
            size=output.shape[0] + 1,
            mode="linear",
            align_corners=True,
        )[0, 0, :-1]
        output_rms = torch.max(output_rms, torch.zeros_like(output_rms) + 1e-3)
        return output * torch.pow(
            input_rms / output_rms,
            torch.tensor(1 - self.cfg.rms_mix_rate, device=self.device),
        )

    def _apply_sola(self, output: torch.Tensor) -> torch.Tensor:
        correlation_input = output[
            None,
            None,
            : self.sola_buffer_frame + self.sola_search_frame,
        ]
        correlation = F.conv1d(correlation_input, self.sola_buffer[None, None, :])
        normalization = torch.sqrt(
            F.conv1d(
                correlation_input**2,
                torch.ones(1, 1, self.sola_buffer_frame, device=self.device),
            )
            + 1e-8
        )
        sola_offset = torch.argmax(correlation[0, 0] / normalization[0, 0])
        output = output[sola_offset:]
        output[: self.sola_buffer_frame] *= self.fade_in_window
        output[: self.sola_buffer_frame] += self.sola_buffer * self.fade_out_window
        self.sola_buffer[:] = output[
            self.block_frame : self.block_frame + self.sola_buffer_frame
        ]
        return output


def _load_runtime_dependencies() -> None:
    global F, RVC, librosa, np, tat, torch

    import librosa
    import numpy as np
    import torch
    import torch.nn.functional as F
    import torchaudio.transforms as tat

    from tools.rvc_for_realtime import RVC


def _build_runtime_config(cfg: AudioEngineConfig):
    from configs.config import Config

    argv = sys.argv[:]
    sys.argv = sys.argv[:1]
    try:
        config = Config()
    finally:
        sys.argv = argv
    if cfg.device:
        config.device = cfg.device
    if cfg.is_half is not None:
        config.is_half = cfg.is_half
    config.use_jit = cfg.use_jit
    return config
