"""Push-to-talk voice pipeline for VOCO (CPU-oriented, lazy ASR)."""

from __future__ import annotations

import gc
import os
import time
from typing import Callable

import constants


def _normalize_bootstrap_path(path_value: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(str(path_value)))
    return os.path.abspath(expanded)


def _default_runtime_root() -> str:
    configured_root = str(os.getenv("VOCO_RUNTIME_ROOT", "")).strip()
    if configured_root:
        return _normalize_bootstrap_path(configured_root)
    if os.path.isdir("O:\\"):
        return _normalize_bootstrap_path("O:\\voco-runtime")
    return _normalize_bootstrap_path(os.path.join(os.path.dirname(__file__), "..", ".voco-runtime"))


def _bootstrap_voice_cache_env() -> None:
    runtime_root = _default_runtime_root()
    hf_home = str(os.getenv("HF_HOME", "")).strip() or os.path.join(runtime_root, "huggingface")
    hf_hub_cache = str(os.getenv("HF_HUB_CACHE", "")).strip() or os.path.join(hf_home, "hub")

    paths = [
        _normalize_bootstrap_path(runtime_root),
        _normalize_bootstrap_path(hf_home),
        _normalize_bootstrap_path(hf_hub_cache),
    ]
    for path_value in paths:
        try:
            os.makedirs(path_value, exist_ok=True)
        except Exception:
            continue

    os.environ["VOCO_RUNTIME_ROOT"] = _normalize_bootstrap_path(runtime_root)
    os.environ["HF_HOME"] = _normalize_bootstrap_path(hf_home)
    os.environ["HF_HUB_CACHE"] = _normalize_bootstrap_path(hf_hub_cache)


_bootstrap_voice_cache_env()

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional runtime dependency
    np = None

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover - optional runtime dependency
    sd = None

try:
    from faster_whisper import WhisperModel as FasterWhisperModel
except ImportError:  # pragma: no cover - optional runtime dependency
    FasterWhisperModel = None


class VocoVoice:
    """Voice listener with push-to-talk transcription mode only."""

    SAMPLE_RATE = 16000
    COMMAND_FRAME_SIZE = 160
    INTERACTION_MODE_PUSH_TO_TALK = "push_to_talk"
    INTERACTION_MODES = {INTERACTION_MODE_PUSH_TO_TALK}
    DEFAULT_WHISPER_MODEL = constants.VOICE_MODEL_ID
    VOICE_BASE_INSTALL_HINT = "pip install sounddevice numpy faster-whisper"
    VOCO_RUNTIME_ROOT_ENV = "VOCO_RUNTIME_ROOT"
    HF_HOME_ENV = "HF_HOME"
    HF_HUB_CACHE_ENV = "HF_HUB_CACHE"
    WHISPER_COMPUTE_TYPE = constants.VOICE_COMPUTE_TYPE

    _DEPENDENCY_PACKAGE_MAP = {
        "sounddevice": "sounddevice",
        "numpy": "numpy",
        "faster_whisper": "faster-whisper",
    }

    @classmethod
    def _build_install_hint(cls, missing: list[str]) -> str:
        packages = [cls._DEPENDENCY_PACKAGE_MAP[name] for name in missing if name in cls._DEPENDENCY_PACKAGE_MAP]
        unique_packages = list(dict.fromkeys(packages))
        if not unique_packages:
            return cls.VOICE_BASE_INSTALL_HINT
        return f"pip install {' '.join(unique_packages)}"

    @classmethod
    def _configure_runtime_paths(cls) -> dict[str, str]:
        runtime_root = str(os.getenv(cls.VOCO_RUNTIME_ROOT_ENV, "")).strip() or _default_runtime_root()
        hf_home = str(os.getenv(cls.HF_HOME_ENV, "")).strip() or os.path.join(runtime_root, "huggingface")
        hf_hub_cache = str(os.getenv(cls.HF_HUB_CACHE_ENV, "")).strip() or os.path.join(hf_home, "hub")
        paths = {
            "runtime_root": _normalize_bootstrap_path(runtime_root),
            "hf_home": _normalize_bootstrap_path(hf_home),
            "hf_hub_cache": _normalize_bootstrap_path(hf_hub_cache),
        }
        for value in paths.values():
            os.makedirs(value, exist_ok=True)
        os.environ[cls.VOCO_RUNTIME_ROOT_ENV] = paths["runtime_root"]
        os.environ[cls.HF_HOME_ENV] = paths["hf_home"]
        os.environ[cls.HF_HUB_CACHE_ENV] = paths["hf_hub_cache"]
        return paths

    @classmethod
    def dependency_status(cls) -> dict[str, object]:
        missing: list[str] = []
        if sd is None:
            missing.append("sounddevice")
        if np is None:
            missing.append("numpy")
        if FasterWhisperModel is None:
            missing.append("faster_whisper")

        status: dict[str, object] = {
            "available": len(missing) == 0,
            "missing": missing,
            "install_hint": cls._build_install_hint(missing) if missing else cls.VOICE_BASE_INSTALL_HINT,
            "whisper_model_default": cls.DEFAULT_WHISPER_MODEL,
            "vad_mode": "ptt-only",
        }
        return status

    @classmethod
    def startup_status(cls) -> dict[str, object]:
        status = cls.dependency_status()
        runtime_paths = cls._configure_runtime_paths()
        status["runtime_root"] = runtime_paths["runtime_root"]
        status["hf_cache"] = runtime_paths["hf_hub_cache"]
        status["wake_model_dir"] = ""
        status["runtime_ready"] = bool(status["available"])
        if not status["available"]:
            status["runtime_hint"] = str(status.get("install_hint", cls.VOICE_BASE_INSTALL_HINT))
        return status

    def __init__(
        self,
        on_command_callback: Callable[[str], None],
        whisper_model_size: str = DEFAULT_WHISPER_MODEL,
        wake_threshold: float | None = None,
        on_status_callback: Callable[[str, str], None] | None = None,
        interaction_mode: str = INTERACTION_MODE_PUSH_TO_TALK,
    ) -> None:
        _ = wake_threshold
        dep_status = self.dependency_status()
        if not dep_status["available"]:
            missing = ", ".join(dep_status["missing"])
            install_hint = str(dep_status.get("install_hint", self.VOICE_BASE_INSTALL_HINT)).strip()
            raise RuntimeError(f"Voice dependencies missing: {missing}. Install with: {install_hint}")

        self._runtime_paths = self._configure_runtime_paths()
        self._whisper_model_size = str(whisper_model_size or self.DEFAULT_WHISPER_MODEL).strip()
        if not self._whisper_model_size:
            self._whisper_model_size = self.DEFAULT_WHISPER_MODEL
        self._interaction_mode = self._normalize_interaction_mode(interaction_mode)
        self._asr_loaded = False
        self._fw_model = None
        self.on_command = on_command_callback
        self._on_status = on_status_callback
        self.vad_mode = "ptt-only"
        self._listening = False
        self._ptt_stream = None
        self._ptt_active = False
        self._ptt_started_at = 0.0
        self._ptt_preallocated_samples = int(constants.VOICE_PREALLOCATE_BUFFER_SEC * self.SAMPLE_RATE)
        self._ptt_max_frames = int(constants.VOICE_PREALLOCATE_BUFFER_SEC * self.SAMPLE_RATE / self.COMMAND_FRAME_SIZE)
        self._ptt_buffer = np.zeros(self._ptt_preallocated_samples, dtype=np.int16)
        self._ptt_buffer_write_pos = 0

    @classmethod
    def _normalize_interaction_mode(cls, interaction_mode: str) -> str:
        mode = str(interaction_mode or cls.INTERACTION_MODE_PUSH_TO_TALK).strip().lower()
        if mode in cls.INTERACTION_MODES:
            return mode
        valid_modes = ", ".join(sorted(cls.INTERACTION_MODES))
        raise ValueError(f"Unsupported interaction mode '{interaction_mode}'. Expected one of: {valid_modes}.")

    def _emit_status(self, message: str, level: str = "info") -> None:
        if self._on_status is None:
            return
        try:
            self._on_status(message, level)
        except Exception:
            return

    def _ensure_asr_loaded(self) -> None:
        if self._asr_loaded:
            return
        if FasterWhisperModel is None:
            raise RuntimeError("faster-whisper is not available for ASR initialization.")
        self._fw_model = FasterWhisperModel(
            self._whisper_model_size,
            device="cpu",
            compute_type=self.WHISPER_COMPUTE_TYPE,
            num_workers=1,
            download_root=self._runtime_paths["runtime_root"],
        )
        self._asr_loaded = True

    def _reset_ptt_buffer(self) -> None:
        self._ptt_buffer.fill(0)
        self._ptt_buffer_write_pos = 0

    def _stop_ptt_stream(self) -> None:
        if self._ptt_stream is not None:
            try:
                self._ptt_stream.stop()
            except Exception:
                pass
            try:
                self._ptt_stream.close()
            except Exception:
                pass
        self._ptt_stream = None
        self._ptt_active = False

    def _ptt_callback(self, indata, frames, time_info, status) -> None:  # pragma: no cover - callback runtime
        _ = frames, time_info
        if status:
            self._emit_status(f"PTT audio status: {status}", "degraded")
        if not self._ptt_active:
            return
        pcm_frame = np.asarray(indata).reshape(-1).astype(np.int16, copy=False)
        available = self._ptt_preallocated_samples - self._ptt_buffer_write_pos
        if available <= 0:
            return
        write_count = min(len(pcm_frame), available)
        if write_count <= 0:
            return
        end = self._ptt_buffer_write_pos + write_count
        self._ptt_buffer[self._ptt_buffer_write_pos : end] = pcm_frame[:write_count]
        self._ptt_buffer_write_pos = end

    def start(self) -> None:
        if self._listening:
            return
        self._listening = True
        self._emit_status("Push-to-talk ready. Hold SPACE to capture speech.", "ready")

    def stop(self) -> None:
        self._stop_ptt_stream()
        self._listening = False
        self._fw_model = None
        self._asr_loaded = False
        gc.collect()

    def begin_push_to_talk(self, max_duration: int = constants.VOICE_PREALLOCATE_BUFFER_SEC) -> bool:
        if self._interaction_mode != self.INTERACTION_MODE_PUSH_TO_TALK:
            self._emit_status("Push-to-talk is only available in push_to_talk interaction mode.", "error")
            return False
        if not self._listening:
            self._emit_status("Voice listener is OFF. Toggle voice before push-to-talk.", "error")
            return False
        now = time.monotonic()
        if self._ptt_started_at > 0 and (now - self._ptt_started_at) < (constants.PTT_DEBOUNCE_MS / 1000):
            return False
        if self._ptt_active:
            return True

        safe_duration = max(1, min(int(max_duration), int(constants.VOICE_PREALLOCATE_BUFFER_SEC)))
        self._ptt_max_frames = int(safe_duration * self.SAMPLE_RATE / self.COMMAND_FRAME_SIZE)
        self._reset_ptt_buffer()
        self._ptt_started_at = now
        try:
            self._ptt_stream = sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=self.COMMAND_FRAME_SIZE,
                callback=self._ptt_callback,
            )
            self._ptt_stream.start()
            self._ptt_active = True
            self._emit_status("Push-to-talk recording started.", "step")
            return True
        except Exception as exc:  # pragma: no cover - runtime/hardware specific
            self._ptt_stream = None
            self._ptt_active = False
            self._emit_status(f"Push-to-talk start failed: {exc}", "error")
            return False

    def end_push_to_talk(self, min_duration_seconds: float = 0.1) -> str:
        if self._interaction_mode != self.INTERACTION_MODE_PUSH_TO_TALK:
            return ""
        if not self._ptt_active:
            return ""

        duration = max(0.0, time.monotonic() - self._ptt_started_at)
        self._stop_ptt_stream()
        if duration < min_duration_seconds:
            self._emit_status("Push-to-talk capture was too short.", "step")
            return ""
        if self._ptt_buffer_write_pos <= 0:
            self._emit_status("Push-to-talk captured no audio.", "step")
            return ""

        frame_count = int(self._ptt_buffer_write_pos / self.COMMAND_FRAME_SIZE)
        if frame_count > self._ptt_max_frames:
            max_samples = self._ptt_max_frames * self.COMMAND_FRAME_SIZE
            audio_samples = self._ptt_buffer[:max_samples].copy()
        else:
            audio_samples = self._ptt_buffer[: self._ptt_buffer_write_pos].copy()
        frames = [audio_samples]
        return self._transcribe_frames(frames)

    def _transcribe_frames(self, frames: list) -> str:
        self._ensure_asr_loaded()
        audio = np.concatenate(frames).astype(np.float32) / 32768.0
        try:
            segments, _ = self._fw_model.transcribe(
                audio,
                language=constants.VOICE_TRANSCRIBE_LANGUAGE,
                beam_size=1,
                vad_filter=True,
                condition_on_previous_text=False,
            )
        except Exception as exc:  # pragma: no cover - runtime/model specific
            self._emit_status(f"Transcription failed ({self._whisper_model_size}): {exc}", "error")
            return ""
        gc.collect()
        return " ".join(str(segment.text).strip() for segment in segments).strip()
