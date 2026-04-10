"""Wake-word + transcription pipeline for VOCO (CPU-oriented)."""

from __future__ import annotations

import os
import threading
from collections import deque
from typing import Callable


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
    transformers_cache = str(os.getenv("TRANSFORMERS_CACHE", "")).strip() or os.path.join(hf_home, "transformers")
    wake_model_dir = str(os.getenv("OPENWAKEWORD_MODEL_DIR", "")).strip() or os.path.join(
        runtime_root,
        "openwakeword",
        "models",
    )

    paths = [
        _normalize_bootstrap_path(runtime_root),
        _normalize_bootstrap_path(hf_home),
        _normalize_bootstrap_path(hf_hub_cache),
        _normalize_bootstrap_path(transformers_cache),
        _normalize_bootstrap_path(wake_model_dir),
    ]
    for path_value in paths:
        try:
            os.makedirs(path_value, exist_ok=True)
        except Exception:
            continue

    os.environ["VOCO_RUNTIME_ROOT"] = _normalize_bootstrap_path(runtime_root)
    os.environ["HF_HOME"] = _normalize_bootstrap_path(hf_home)
    os.environ["HF_HUB_CACHE"] = _normalize_bootstrap_path(hf_hub_cache)
    os.environ["TRANSFORMERS_CACHE"] = _normalize_bootstrap_path(transformers_cache)
    os.environ["OPENWAKEWORD_MODEL_DIR"] = _normalize_bootstrap_path(wake_model_dir)


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
    from transformers import pipeline as transformers_pipeline
except ImportError:  # pragma: no cover - optional runtime dependency
    transformers_pipeline = None

try:
    import torch
except ImportError:  # pragma: no cover - optional runtime dependency
    torch = None

try:
    from openwakeword.model import Model as OpenWakeWordModel
except ImportError:  # pragma: no cover - optional runtime dependency
    try:
        from openwakeword import Model as OpenWakeWordModel
    except ImportError:  # pragma: no cover - optional runtime dependency
        OpenWakeWordModel = None

try:
    import openwakeword
except ImportError:  # pragma: no cover - optional runtime dependency
    openwakeword = None

try:
    from openwakeword.utils import download_models as download_openwakeword_models
except ImportError:  # pragma: no cover - optional runtime dependency
    download_openwakeword_models = None

try:
    import webrtcvad
except ImportError:  # pragma: no cover - optional runtime dependency
    webrtcvad = None


class VocoVoice:
    """Wake-word listener with openwakeword + Hugging Face Whisper transcription."""

    SAMPLE_RATE = 16000
    WAKE_BLOCK_SIZE = 1280  # 80ms (openwakeword-preferred chunk size)
    COMMAND_FRAME_SIZE = 160  # 10ms (valid WebRTC VAD frame at 16kHz)
    WAKE_THRESHOLD = 0.5
    SILENCE_THRESHOLD = 0.012
    TRAILING_SILENCE_SECONDS = 0.9
    WAKE_LABEL_HINT = "jarvis"
    WHISPER_MODEL_ID = "openai/whisper-medium"
    DEFAULT_WHISPER_MODEL = WHISPER_MODEL_ID
    VOICE_BASE_INSTALL_HINT = "pip install openwakeword sounddevice numpy transformers torch"
    VOICE_VAD_INSTALL_HINT = "pip install webrtcvad"
    WAKE_ONNX_RUNTIME_HINT = (
        "Wake model ONNX init failed. Install ONNX runtime with: pip install onnxruntime. "
        "Ensure openwakeword ONNX models are available in OPENWAKEWORD_MODEL_DIR."
    )
    WAKE_TFLITE_FALLBACK_ENV = "VOCO_WAKE_ALLOW_TFLITE_FALLBACK"
    WAKE_TFLITE_INSTALL_HINT = "pip install tflite-runtime"
    VOCO_RUNTIME_ROOT_ENV = "VOCO_RUNTIME_ROOT"
    HF_HOME_ENV = "HF_HOME"
    HF_HUB_CACHE_ENV = "HF_HUB_CACHE"
    TRANSFORMERS_CACHE_ENV = "TRANSFORMERS_CACHE"
    OPENWAKEWORD_MODEL_DIR_ENV = "OPENWAKEWORD_MODEL_DIR"
    _RUNTIME_POLICY_HINT = (
        "Set VOCO_RUNTIME_ROOT or cache env vars (HF_HOME/HF_HUB_CACHE/OPENWAKEWORD_MODEL_DIR) "
        "to a writable non-C path."
    )
    _DEPENDENCY_PACKAGE_MAP = {
        "openwakeword": "openwakeword",
        "sounddevice": "sounddevice",
        "numpy": "numpy",
        "transformers": "transformers",
        "torch": "torch",
    }

    @classmethod
    def _build_install_hint(cls, missing: list[str]) -> str:
        packages = [cls._DEPENDENCY_PACKAGE_MAP[name] for name in missing if name in cls._DEPENDENCY_PACKAGE_MAP]
        unique_packages = list(dict.fromkeys(packages))
        if not unique_packages:
            return cls.VOICE_BASE_INSTALL_HINT
        return f"pip install {' '.join(unique_packages)}"

    @classmethod
    def _allow_tflite_fallback(cls) -> bool:
        return str(os.getenv(cls.WAKE_TFLITE_FALLBACK_ENV, "")).strip() == "1"

    @staticmethod
    def _normalize_path(path_value: str) -> str:
        expanded = os.path.expandvars(os.path.expanduser(path_value))
        return os.path.abspath(expanded)

    @staticmethod
    def _is_c_drive(path_value: str) -> bool:
        return os.path.splitdrive(os.path.abspath(path_value))[0].upper() == "C:"

    @classmethod
    def _resolve_runtime_root(cls) -> str:
        configured_root = str(os.getenv(cls.VOCO_RUNTIME_ROOT_ENV, "")).strip()
        if configured_root:
            return cls._normalize_path(configured_root)
        return _default_runtime_root()

    @classmethod
    def _configure_runtime_paths(cls) -> dict[str, str]:
        runtime_root = cls._resolve_runtime_root()
        hf_home = str(os.getenv(cls.HF_HOME_ENV, "")).strip() or os.path.join(runtime_root, "huggingface")
        hf_hub_cache = str(os.getenv(cls.HF_HUB_CACHE_ENV, "")).strip() or os.path.join(hf_home, "hub")
        transformers_cache = str(os.getenv(cls.TRANSFORMERS_CACHE_ENV, "")).strip() or os.path.join(
            hf_home,
            "transformers",
        )
        wake_model_dir = str(os.getenv(cls.OPENWAKEWORD_MODEL_DIR_ENV, "")).strip() or os.path.join(
            runtime_root,
            "openwakeword",
            "models",
        )

        paths = {
            "runtime_root": cls._normalize_path(runtime_root),
            "hf_home": cls._normalize_path(hf_home),
            "hf_hub_cache": cls._normalize_path(hf_hub_cache),
            "transformers_cache": cls._normalize_path(transformers_cache),
            "wake_model_dir": cls._normalize_path(wake_model_dir),
        }

        c_drive_paths = {
            key: value for key, value in paths.items() if key != "runtime_root" and cls._is_c_drive(value)
        }
        if c_drive_paths:
            details = ", ".join(f"{key}={value}" for key, value in c_drive_paths.items())
            raise RuntimeError(f"Runtime path policy violation (C drive): {details}. {cls._RUNTIME_POLICY_HINT}")

        for path_value in paths.values():
            os.makedirs(path_value, exist_ok=True)

        os.environ[cls.VOCO_RUNTIME_ROOT_ENV] = paths["runtime_root"]
        os.environ[cls.HF_HOME_ENV] = paths["hf_home"]
        os.environ[cls.HF_HUB_CACHE_ENV] = paths["hf_hub_cache"]
        os.environ[cls.TRANSFORMERS_CACHE_ENV] = paths["transformers_cache"]
        os.environ[cls.OPENWAKEWORD_MODEL_DIR_ENV] = paths["wake_model_dir"]
        return paths

    @staticmethod
    def _replace_model_extension(download_url: str, extension: str) -> str:
        filename = os.path.basename(download_url)
        stem, _ = os.path.splitext(filename)
        return f"{stem}{extension}"

    @classmethod
    def _openwakeword_assets(cls, wake_model_dir: str) -> dict[str, object]:
        if openwakeword is None:
            raise RuntimeError("openwakeword metadata is unavailable. Reinstall openwakeword.")

        wake_urls = [str(meta.get("download_url", "")) for meta in getattr(openwakeword, "MODELS", {}).values()]
        wake_urls = [url for url in wake_urls if url]
        if not wake_urls:
            raise RuntimeError("openwakeword model metadata is empty. Reinstall openwakeword.")

        wake_models_onnx = [os.path.join(wake_model_dir, cls._replace_model_extension(url, ".onnx")) for url in wake_urls]
        wake_models_tflite = [
            os.path.join(wake_model_dir, cls._replace_model_extension(url, ".tflite")) for url in wake_urls
        ]

        feature_urls = {
            str(name): str(meta.get("download_url", ""))
            for name, meta in getattr(openwakeword, "FEATURE_MODELS", {}).items()
            if str(meta.get("download_url", "")).strip()
        }
        feature_onnx = {
            key: os.path.join(wake_model_dir, cls._replace_model_extension(url, ".onnx"))
            for key, url in feature_urls.items()
        }
        feature_tflite = {
            key: os.path.join(wake_model_dir, cls._replace_model_extension(url, ".tflite"))
            for key, url in feature_urls.items()
        }

        melspec_onnx = next((path for key, path in feature_onnx.items() if "melspectrogram" in key.lower()), "")
        embedding_onnx = next((path for key, path in feature_onnx.items() if "embedding" in key.lower()), "")
        melspec_tflite = next((path for key, path in feature_tflite.items() if "melspectrogram" in key.lower()), "")
        embedding_tflite = next((path for key, path in feature_tflite.items() if "embedding" in key.lower()), "")

        required_onnx = list(dict.fromkeys([*wake_models_onnx, melspec_onnx, embedding_onnx]))
        required_onnx = [path for path in required_onnx if path]
        required_tflite = list(dict.fromkeys([*wake_models_tflite, melspec_tflite, embedding_tflite]))
        required_tflite = [path for path in required_tflite if path]

        return {
            "wake_models_onnx": wake_models_onnx,
            "wake_models_tflite": wake_models_tflite,
            "melspec_onnx": melspec_onnx,
            "embedding_onnx": embedding_onnx,
            "melspec_tflite": melspec_tflite,
            "embedding_tflite": embedding_tflite,
            "required_onnx": required_onnx,
            "required_tflite": required_tflite,
        }

    @classmethod
    def _provision_openwakeword_assets(cls, wake_model_dir: str) -> dict[str, object]:
        assets = cls._openwakeword_assets(wake_model_dir)
        missing = [path for path in assets["required_onnx"] if not os.path.exists(path)]
        if not missing:
            return assets

        if download_openwakeword_models is None:
            raise RuntimeError(
                "openwakeword model downloader is unavailable. Reinstall openwakeword so models can be prefetched."
            )

        try:
            download_openwakeword_models(target_directory=wake_model_dir)
        except TypeError:
            download_openwakeword_models([], wake_model_dir)
        except Exception as exc:  # pragma: no cover - network/runtime specific
            raise RuntimeError(f"Failed to prefetch openwakeword models into '{wake_model_dir}': {exc}") from exc

        still_missing = [path for path in assets["required_onnx"] if not os.path.exists(path)]
        if still_missing:
            missing_preview = ", ".join(os.path.basename(path) for path in still_missing[:4])
            raise RuntimeError(
                f"openwakeword ONNX assets are still missing in '{wake_model_dir}' ({missing_preview})."
            )
        return assets

    @classmethod
    def _create_wake_model_with_assets(cls, assets: dict[str, object]):
        onnx_kwargs = {
            "inference_framework": "onnx",
            "wakeword_models": list(assets["wake_models_onnx"]),
        }
        if assets["melspec_onnx"]:
            onnx_kwargs["melspec_model_path"] = str(assets["melspec_onnx"])
        if assets["embedding_onnx"]:
            onnx_kwargs["embedding_model_path"] = str(assets["embedding_onnx"])

        try:
            wake_model = OpenWakeWordModel(**onnx_kwargs)
            return wake_model, "onnx"
        except Exception as onnx_error:  # pragma: no cover - runtime/env specific
            if not cls._allow_tflite_fallback():
                raise RuntimeError(
                    "Failed to initialize openwakeword model. "
                    f"{cls.WAKE_ONNX_RUNTIME_HINT} Set {cls.WAKE_TFLITE_FALLBACK_ENV}=1 to try default framework fallback."
                ) from onnx_error

        tflite_kwargs = {
            "wakeword_models": list(assets["wake_models_tflite"]),
        }
        if assets["melspec_tflite"]:
            tflite_kwargs["melspec_model_path"] = str(assets["melspec_tflite"])
        if assets["embedding_tflite"]:
            tflite_kwargs["embedding_model_path"] = str(assets["embedding_tflite"])

        try:
            wake_model = OpenWakeWordModel(**tflite_kwargs)
            return wake_model, "tflite"
        except Exception as fallback_error:  # pragma: no cover - runtime/env specific
            fallback_note = (
                f"Default-framework fallback failed ({cls.WAKE_TFLITE_FALLBACK_ENV}=1). "
                f"If you rely on this fallback, install with: {cls.WAKE_TFLITE_INSTALL_HINT}"
            )
            raise RuntimeError(
                f"Failed to initialize openwakeword model. {cls.WAKE_ONNX_RUNTIME_HINT} {fallback_note}"
            ) from fallback_error

    @classmethod
    def dependency_status(cls) -> dict[str, object]:
        missing = []
        if OpenWakeWordModel is None:
            missing.append("openwakeword")
        if sd is None:
            missing.append("sounddevice")
        if np is None:
            missing.append("numpy")
        if transformers_pipeline is None:
            missing.append("transformers")
        if torch is None:
            missing.append("torch")

        install_hint = cls._build_install_hint(missing)
        return {
            "available": len(missing) == 0,
            "missing": missing,
            "vad_mode": "webrtcvad" if webrtcvad is not None else "silence-heuristic",
            "install_hint": install_hint,
            "vad_install_hint": cls.VOICE_VAD_INSTALL_HINT,
        }

    @classmethod
    def startup_status(cls) -> dict[str, object]:
        status = cls.dependency_status()
        status["runtime_ready"] = False
        status["error"] = ""
        status["runtime_hint"] = ""
        status["fallback_attempted"] = False
        status["fallback_error"] = ""
        status["fallback_note"] = ""
        status["whisper_model_default"] = cls.WHISPER_MODEL_ID

        try:
            runtime_paths = cls._configure_runtime_paths()
        except Exception as exc:
            status["error"] = str(exc)
            status["runtime_hint"] = cls._RUNTIME_POLICY_HINT
            return status

        status["runtime_root"] = runtime_paths["runtime_root"]
        status["hf_cache"] = runtime_paths["hf_hub_cache"]
        status["wake_model_dir"] = runtime_paths["wake_model_dir"]

        if not status["available"]:
            status["runtime_hint"] = str(status.get("install_hint", cls.VOICE_BASE_INSTALL_HINT))
            return status

        try:
            assets = cls._provision_openwakeword_assets(runtime_paths["wake_model_dir"])
            wake_model, wake_framework = cls._create_wake_model_with_assets(assets)
            status["runtime_ready"] = True
            status["wake_framework"] = wake_framework
            status["wake_model_count"] = len(assets["wake_models_onnx"])
            del wake_model
            return status
        except Exception as exc:  # pragma: no cover - runtime/env specific
            status["error"] = str(exc)
            status["runtime_hint"] = (
                f"{cls.WAKE_ONNX_RUNTIME_HINT} Runtime cache: {runtime_paths['wake_model_dir']}"
            )
            if cls._allow_tflite_fallback():
                status["fallback_attempted"] = True
            return status

    def __init__(
        self,
        on_command_callback: Callable[[str], None],
        whisper_model_size: str = DEFAULT_WHISPER_MODEL,
        wake_threshold: float | None = None,
        on_status_callback: Callable[[str, str], None] | None = None,
    ):
        dep_status = self.dependency_status()
        if not dep_status["available"]:
            missing = ", ".join(dep_status["missing"])
            install_hint = str(dep_status.get("install_hint", self.VOICE_BASE_INSTALL_HINT)).strip()
            raise RuntimeError(f"Voice dependencies missing: {missing}. Install with: {install_hint}")

        self._runtime_paths = self._configure_runtime_paths()
        self._wake_assets = self._provision_openwakeword_assets(self._runtime_paths["wake_model_dir"])
        self._wake_model = self._create_wake_model()
        self._whisper_model_size = str(whisper_model_size or self.WHISPER_MODEL_ID).strip() or self.WHISPER_MODEL_ID
        self.whisper = self._create_asr_pipeline(self._whisper_model_size)
        self.on_command = on_command_callback
        self._on_status = on_status_callback
        self._wake_threshold = self.WAKE_THRESHOLD if wake_threshold is None else wake_threshold
        self._vad = webrtcvad.Vad(2) if webrtcvad is not None else None
        self.vad_mode = "webrtcvad" if self._vad is not None else "silence-heuristic"
        self._listening = False
        self._thread: threading.Thread | None = None

    def _create_asr_pipeline(self, model_id: str):
        try:
            return transformers_pipeline(
                "automatic-speech-recognition",
                model=model_id,
                tokenizer=model_id,
                feature_extractor=model_id,
                device=-1,
                model_kwargs={"cache_dir": self._runtime_paths["hf_hub_cache"]},
            )
        except Exception as exc:
            hint = (
                f"Failed to initialize Whisper ASR model '{model_id}'. "
                f"Install or repair dependencies with: {self.VOICE_BASE_INSTALL_HINT}. "
                f"Cache path: {self._runtime_paths['hf_hub_cache']}"
            )
            raise RuntimeError(f"{hint}. Runtime error: {exc}") from exc

    def _create_wake_model(self):
        wake_model, wake_framework = self._create_wake_model_with_assets(self._wake_assets)
        self._wake_framework = wake_framework
        return wake_model

    def _emit_status(self, message: str, level: str = "info") -> None:
        if self._on_status is None:
            return
        try:
            self._on_status(message, level)
        except Exception:
            return

    def start(self) -> None:
        if self._listening:
            return
        self._listening = True
        self._thread = threading.Thread(target=self._wake_word_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._listening = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None

    def _wake_word_loop(self) -> None:
        self._emit_status(
            (
                f"Wake listener active ({self.vad_mode}, wake:{getattr(self, '_wake_framework', 'onnx')}, "
                f"asr:{self._whisper_model_size})."
            ),
            "ready" if self.vad_mode == "webrtcvad" else "degraded",
        )
        try:
            with sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=self.WAKE_BLOCK_SIZE,
            ) as stream:
                while self._listening:
                    frame, _ = stream.read(self.WAKE_BLOCK_SIZE)
                    if not self._is_wake_word_detected(frame):
                        continue

                    self._emit_status("Wake word detected. Listening for command...", "step")
                    command = self._capture_command(stream)
                    if command:
                        self.on_command(command)
        except Exception as exc:  # pragma: no cover - runtime/hardware specific
            self._listening = False
            self._emit_status(f"Voice runtime stopped: {exc}", "error")

    def _is_wake_word_detected(self, frame) -> bool:
        pcm = np.asarray(frame).reshape(-1).astype(np.int16, copy=False)
        predictions = self._wake_model.predict(pcm)
        if not isinstance(predictions, dict) or not predictions:
            return False

        labels = [label for label in predictions if self.WAKE_LABEL_HINT in label.lower()]
        if not labels:
            labels = list(predictions.keys())

        best_label = max(labels, key=lambda label: float(predictions.get(label, 0.0)))
        best_score = float(predictions.get(best_label, 0.0))
        return best_score >= self._wake_threshold

    def _capture_command(self, stream, max_seconds: int = 8) -> str:
        frames = []
        pre_roll = deque(maxlen=int(0.30 * self.SAMPLE_RATE / self.COMMAND_FRAME_SIZE))
        speech_started = False
        silence_frames = 0
        max_silence_frames = int(self.TRAILING_SILENCE_SECONDS * self.SAMPLE_RATE / self.COMMAND_FRAME_SIZE)
        max_frames = int(max_seconds * self.SAMPLE_RATE / self.COMMAND_FRAME_SIZE)

        for _ in range(max_frames):
            if not self._listening:
                break

            frame, _ = stream.read(self.COMMAND_FRAME_SIZE)
            pcm_frame = np.asarray(frame).reshape(-1).astype(np.int16, copy=False)
            pre_roll.append(pcm_frame.copy())
            is_speech = self._is_speech_frame(pcm_frame)

            if is_speech:
                if not speech_started:
                    speech_started = True
                    frames.extend(list(pre_roll))
                    pre_roll.clear()
                else:
                    frames.append(pcm_frame.copy())
                silence_frames = 0
                continue

            if speech_started:
                frames.append(pcm_frame.copy())
                silence_frames += 1
                if silence_frames >= max_silence_frames:
                    break

        if not speech_started or not frames:
            return ""

        audio = np.concatenate(frames).astype(np.float32) / 32768.0
        try:
            result = self.whisper(
                {
                    "raw": audio,
                    "sampling_rate": self.SAMPLE_RATE,
                }
            )
        except Exception as exc:  # pragma: no cover - runtime/model specific
            self._emit_status(
                (
                    f"Transcription failed ({self._whisper_model_size}): {exc}. "
                    f"Check transformers setup: {self.VOICE_BASE_INSTALL_HINT}"
                ),
                "error",
            )
            return ""

        if isinstance(result, dict):
            return str(result.get("text", "")).strip()
        if isinstance(result, str):
            return result.strip()
        return ""

    def _is_speech_frame(self, pcm_frame) -> bool:
        if self._vad is not None:
            try:
                return bool(self._vad.is_speech(pcm_frame.tobytes(), self.SAMPLE_RATE))
            except Exception:  # pragma: no cover - runtime edge case
                self._vad = None
                self.vad_mode = "silence-heuristic"
                self._emit_status("webrtcvad failed, switched to silence heuristic VAD.", "degraded")

        amplitude = float(np.abs(pcm_frame.astype(np.int32)).mean()) / 32768.0
        return amplitude >= self.SILENCE_THRESHOLD
