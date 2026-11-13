import io
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import sounddevice as sd
import webrtcvad
from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .i18n import init_i18n, set_language, tr

# Initialiseer vertalingen vanuit locales/ op basis van opgeslagen config
CONFIG_DIR = Path.home() / ".config" / "wayland-dictate"
CONFIG_PATH = CONFIG_DIR / "config.json"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
SOCKET_PATH = RUNTIME_DIR / "wayland_dictate.sock"

# Laad tijdelijk config in om initiële taal te bepalen (default: en)
_init_lang = "en"
if CONFIG_PATH.exists():
    try:
        _init_lang = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("ui_language", "en")
    except Exception:
        pass
init_i18n(default_lang=_init_lang)

DEFAULT_CLEANUP_PROMPT = (
    "Je bent een stille tekstcorrector voor live dicteren. "
    "Verwijder alle stopwoorden en twijfelgeluiden (zoals uhm, eh, zeg maar, eigenlijk, dus, nou) "
    "en verwerk zelfcorrecties (bijv. morgen nee overmorgen -> overmorgen) op basis van de context. "
    "Behoud de exacte betekenis, toon en taal (Nederlands). "
    "Geef UITSLUITEND de gecorrigeerde zin terug, zonder aanhalingstekens, uitleg of voorvoegsels."
)

DEFAULT_TRANSLATE_PROMPT = (
    "You are a real-time speech translator. "
    "Remove filler words and hesitations (such as uhm, eh, hmm) and resolve self-corrections, "
    "then translate the spoken text accurately into natural, fluent English. "
    "Preserve the original meaning and tone. "
    "Output ONLY the final English text without quotes, notes, or prefixes."
)

DEFAULT_CONFIG = {
    "ui_language": "en",
    "llm_url": "http://localhost:13305/api/v1",
    "api_key": "",
    "source_language": "nl",
    "dictation_mode": "cleanup",  # "cleanup" | "translate_en"
    "whisper_model": "Whisper-Large-v3-Turbo",
    "whisper_backend": "rocm",
    "npu_polish_enabled": True,
    "llm_model": "gemma4-it-e4b-FLM",
    "llm_temperature": 0.0,
    "llm_max_tokens": 512,
    "min_similarity": 0.62,
    "system_prompt": DEFAULT_CLEANUP_PROMPT,
    "translate_prompt": DEFAULT_TRANSLATE_PROMPT,
    "rms_threshold": 50,
    "vad_aggressiveness": 2,
    "trigger_frames": 3,
    "partial_interval_sec": 0.75,
    "silence_timeout_sec": 0.65,
    "xkb_layout": "us",
    "xkb_variant": "altgr-intl",
    "input_device": "default",
    "shortcut": "Ctrl+Shift+Space",
    "auto_listen_on_start": False,
}

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)

FILLER_RE = re.compile(r"\b(u+h+m*|e+h+m*|h+m+|e+u+h+)\b[,.]?\s*", flags=re.IGNORECASE)
DUP_WORD_RE = re.compile(r"\b(\w+)\s+\1\b", flags=re.IGNORECASE)


def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(saved)
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


class DotoolTyper:
    def __init__(self, layout: str, variant: str) -> None:
        self.layout = layout
        self.variant = variant
        self.typed_buffer = ""
        self.lock = threading.Lock()
        self.proc = self._spawn()

    def _spawn(self) -> subprocess.Popen:
        env = os.environ.copy()
        env["DOTOOL_XKB_LAYOUT"] = self.layout or "us"
        if self.variant:
            env["DOTOOL_XKB_VARIANT"] = self.variant
        else:
            env.pop("DOTOOL_XKB_VARIANT", None)
        return subprocess.Popen(
            ["dotool"],
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )

    def restart(self, layout: str, variant: str) -> None:
        with self.lock:
            self.layout = layout
            self.variant = variant
            self.typed_buffer = ""
            self._close_unlocked()
            self.proc = self._spawn()

    def reset_buffer(self) -> None:
        with self.lock:
            self.typed_buffer = ""

    def apply_diff(self, new_text: str, is_final: bool = False) -> str:
        cleaned = self._quick_clean(new_text)
        if not cleaned:
            return ""
        if is_final:
            cleaned += " "

        with self.lock:
            if self.proc.poll() is not None:
                self.proc = self._spawn()

            common_len = 0
            max_len = min(len(self.typed_buffer), len(cleaned))
            while common_len < max_len and self.typed_buffer[common_len] == cleaned[common_len]:
                common_len += 1

            backspaces = len(self.typed_buffer) - common_len
            to_type = cleaned[common_len:]

            cmds = []
            if backspaces > 0:
                cmds.extend(["key backspace"] * backspaces)
            if to_type:
                cmds.append(f"type {to_type}")

            if cmds and self.proc.stdin:
                self.proc.stdin.write("\n".join(cmds) + "\n")
                self.proc.stdin.flush()

            self.typed_buffer = "" if is_final else cleaned
        return cleaned.strip()

    @staticmethod
    def _quick_clean(text: str) -> str:
        text = text.replace("\n", " ").strip()
        text = FILLER_RE.sub("", text)
        text = DUP_WORD_RE.sub(r"\1", text)
        text = re.sub(r"\s{2,}", " ", text)
        return text.strip()

    def _close_unlocked(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
        except Exception:
            pass

    def close(self) -> None:
        with self.lock:
            self._close_unlocked()


def pcm_to_wav_bytes(pcm_int16: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_int16.tobytes())
    return buf.getvalue()


class EngineSignals(QObject):
    rms_updated = Signal(int)
    state_changed = Signal(str)
    transcript_logged = Signal(str, bool)
    status_message = Signal(str)
    models_loaded = Signal(list)


class DictationEngine:
    def __init__(self, cfg: dict, signals: EngineSignals) -> None:
        self.cfg = cfg
        self.signals = signals
        self.typer = DotoolTyper(cfg["xkb_layout"], cfg["xkb_variant"])

        self.listening = False
        self.speaking = False
        self.stop_event = threading.Event()
        self.cfg_lock = threading.Lock()

        self.audio_q: queue.Queue[np.ndarray] = queue.Queue()
        self.job_q: queue.Queue[tuple[np.ndarray, bool]] = queue.Queue()

        self._audio_thread = threading.Thread(target=self._audio_loop, daemon=True)
        self._worker_thread = threading.Thread(target=self._inference_loop, daemon=True)

    def start_threads(self) -> None:
        self._audio_thread.start()
        self._worker_thread.start()
        threading.Thread(target=self.preload_models, daemon=True).start()

    def update_config(self, new_cfg: dict) -> None:
        with self.cfg_lock:
            layout_changed = (
                new_cfg["xkb_layout"] != self.cfg["xkb_layout"]
                or new_cfg["xkb_variant"] != self.cfg["xkb_variant"]
            )
            self.cfg = new_cfg.copy()
        if layout_changed:
            self.typer.restart(new_cfg["xkb_layout"], new_cfg["xkb_variant"])

    def set_listening(self, active: bool) -> None:
        if self.listening == active:
            return
        self.listening = active
        self.speaking = False
        self.typer.reset_buffer()
        while not self.job_q.empty():
            try:
                self.job_q.get_nowait()
            except queue.Empty:
                break
        self.signals.state_changed.emit("listening" if active else "paused")

    def toggle_listening(self) -> bool:
        self.set_listening(not self.listening)
        return self.listening

    def _auth_headers(self, api_key: str) -> dict[str, str]:
        key = (api_key or "").strip()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def preload_models(self) -> None:
        with self.cfg_lock:
            url = self.cfg["llm_url"].rstrip("/")
            api_key = self.cfg.get("api_key", "").strip()
            headers = self._auth_headers(api_key)
            w_model = self.cfg["whisper_model"]
            w_backend = self.cfg["whisper_backend"]
            l_model = self.cfg["llm_model"]
            npu_on = self.cfg["npu_polish_enabled"]

        try:
            with httpx.Client(headers=headers) as client:
                r = client.get(f"{url}/models", timeout=5.0)
                if r.status_code == 200:
                    models = [m["id"] for m in r.json().get("data", []) if "id" in m]
                    self.signals.models_loaded.emit(models)

                if "localhost" in url or "127.0.0.1" in url:
                    client.post(
                        f"{url}/load",
                        json={"model_name": w_model, "whispercpp_backend": w_backend},
                        timeout=30.0,
                    )
                    if npu_on and l_model:
                        client.post(f"{url}/load", json={"model_name": l_model}, timeout=30.0)
            self.signals.status_message.emit("API / llm connected.")
        except Exception as exc:
            self.signals.status_message.emit(f"Endpoint warning: {exc}")

    def _resolve_device_index(self, device_name: str):
        if not device_name or device_name in ("default", "pipewire", "pulse") or "(hw:" in device_name:
            for preferred in ("pipewire", "default", "pulse"):
                for idx, dev in enumerate(sd.query_devices()):
                    if dev["max_input_channels"] > 0 and dev["name"].lower() == preferred:
                        return idx
            return None
        try:
            for idx, dev in enumerate(sd.query_devices()):
                if dev["max_input_channels"] > 0 and dev["name"] == device_name:
                    return idx
        except Exception:
            pass
        return None

    def _audio_loop(self) -> None:
        last_rms_emit = 0.0
        current_device = None

        while not self.stop_event.is_set():
            with self.cfg_lock:
                desired_device = self.cfg.get("input_device", "default")
            dev_idx = self._resolve_device_index(desired_device)
            current_device = desired_device

            speech_frames: list[np.ndarray] = []
            pre_roll: list[np.ndarray] = []
            speech_streak = 0
            in_speech = False
            last_voice_time = 0.0
            last_partial_time = 0.0
            current_vad_level = -1
            vad = webrtcvad.Vad(2)

            def callback(indata: np.ndarray, frames: int, time_info, status) -> None:
                self.audio_q.put(indata[:, 0].copy())

            try:
                with sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="int16",
                    blocksize=FRAME_SAMPLES,
                    device=dev_idx,
                    callback=callback,
                ):
                    while not self.stop_event.is_set():
                        with self.cfg_lock:
                            if self.cfg.get("input_device", "default") != current_device:
                                break
                            rms_thresh = int(self.cfg["rms_threshold"])
                            vad_level = int(self.cfg["vad_aggressiveness"])
                            trig_frames = int(self.cfg["trigger_frames"])
                            part_sec = float(self.cfg["partial_interval_sec"])
                            sil_sec = float(self.cfg["silence_timeout_sec"])

                        if vad_level != current_vad_level:
                            vad = webrtcvad.Vad(vad_level)
                            current_vad_level = vad_level

                        try:
                            frame = self.audio_q.get(timeout=0.1)
                        except queue.Empty:
                            continue

                        now = time.monotonic()
                        rms = int(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
                        if now - last_rms_emit >= 0.06:
                            last_rms_emit = now
                            self.signals.rms_updated.emit(rms)

                        if not self.listening:
                            if in_speech:
                                in_speech = False
                                speech_frames = []
                                speech_streak = 0
                            continue

                        active = rms >= rms_thresh and vad.is_speech(frame.tobytes(), SAMPLE_RATE)

                        if active:
                            speech_streak += 1
                            if not in_speech:
                                pre_roll.append(frame)
                                if len(pre_roll) > 12:
                                    pre_roll.pop(0)
                                if speech_streak >= trig_frames:
                                    in_speech = True
                                    self.speaking = True
                                    self.signals.state_changed.emit("speaking")
                                    speech_frames = list(pre_roll)
                                    last_partial_time = now
                                    last_voice_time = now
                            else:
                                speech_frames.append(frame)
                                last_voice_time = now
                                if now - last_partial_time >= part_sec:
                                    last_partial_time = now
                                    self.job_q.put((np.concatenate(speech_frames), False))
                        else:
                            speech_streak = 0
                            if in_speech:
                                speech_frames.append(frame)
                                if now - last_voice_time >= sil_sec:
                                    self.job_q.put((np.concatenate(speech_frames), True))
                                    speech_frames = []
                                    in_speech = False
                                    self.speaking = False
                                    if self.listening:
                                        self.signals.state_changed.emit("listening")
                            else:
                                pre_roll.append(frame)
                                if len(pre_roll) > 12:
                                    pre_roll.pop(0)
            except Exception as exc:
                self.signals.status_message.emit(f"Audio error: {exc}")
                time.sleep(1.0)

    def _inference_loop(self) -> None:
        with httpx.Client() as client:
            while not self.stop_event.is_set():
                try:
                    pcm_data, is_final = self.job_q.get(timeout=0.1)
                except queue.Empty:
                    continue

                if not self.listening:
                    continue

                if not is_final:
                    while not self.job_q.empty():
                        next_pcm, next_final = self.job_q.get_nowait()
                        pcm_data, is_final = next_pcm, next_final
                        if is_final:
                            break

                with self.cfg_lock:
                    url = self.cfg["llm_url"].rstrip("/")
                    api_key = self.cfg.get("api_key", "").strip()
                    headers = self._auth_headers(api_key)
                    w_model = self.cfg["whisper_model"]
                    src_lang = self.cfg.get("source_language", "nl")
                    mode = self.cfg.get("dictation_mode", "cleanup")
                    l_model = self.cfg["llm_model"]
                    npu_on = bool(self.cfg["npu_polish_enabled"])
                    sys_prompt = (
                        self.cfg.get("translate_prompt", DEFAULT_TRANSLATE_PROMPT)
                        if mode == "translate_en"
                        else self.cfg["system_prompt"]
                    )
                    llm_temp = float(self.cfg.get("llm_temperature", 0.0))
                    llm_tokens = int(self.cfg.get("llm_max_tokens", 512))
                    min_sim = float(self.cfg.get("min_similarity", 0.62))

                try:
                    wav_bytes = pcm_to_wav_bytes(pcm_data)
                    files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
                    data = {
                        "model": w_model,
                        "language": src_lang,
                        "response_format": "json",
                    }
                    resp = client.post(
                        f"{url}/audio/transcriptions",
                        data=data,
                        files=files,
                        headers=headers,
                        timeout=15.0,
                    )
                    resp.raise_for_status()
                    text = resp.json().get("text", "").strip()
                    if not text or not self.listening:
                        continue

                    should_run_llm = is_final and l_model and (
                        mode == "translate_en" or (npu_on and len(text.split()) >= 3)
                    )
                    if should_run_llm:
                        payload = {
                            "model": l_model,
                            "temperature": llm_temp,
                            "max_tokens": llm_tokens,
                            "messages": [
                                {"role": "system", "content": sys_prompt},
                                {"role": "user", "content": text},
                            ],
                        }
                        llm_resp = client.post(
                            f"{url}/chat/completions",
                            json=payload,
                            headers=headers,
                            timeout=15.0,
                        )
                        llm_resp.raise_for_status()
                        polished = llm_resp.json()["choices"][0]["message"]["content"].strip()
                        if len(polished) >= 2 and polished[0] in (chr(34), chr(39)) and polished[-1] == polished[0]:
                            polished = polished[1:-1].strip()
                        if polished:
                            if mode == "translate_en":
                                text = polished
                            else:
                                ratio = difflib.SequenceMatcher(None, text.lower(), polished.lower()).ratio()
                                if ratio >= min_sim:
                                    text = polished

                    if self.listening:
                        written = self.typer.apply_diff(text, is_final=is_final)
                        if written:
                            self.signals.transcript_logged.emit(written, is_final)
                except Exception as exc:
                    self.signals.status_message.emit(f"Inference error: {exc}")

    def shutdown(self) -> None:
        self.stop_event.set()
        self.typer.close()


def make_tray_icon(state: str) -> QIcon:
    color_map = {
        "paused": QColor("#7f8c8d"),
        "listening": QColor("#2ecc71"),
        "speaking": QColor("#e67e22"),
    }
    bg = color_map.get(state, QColor("#7f8c8d"))
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(bg)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(4, 4, 56, 56)

    p.setBrush(QColor("#ffffff"))
    p.drawRoundedRect(26, 15, 12, 22, 6, 6)
    p.drawRect(30, 39, 4, 8)
    p.drawRoundedRect(22, 46, 20, 4, 2, 2)
    p.end()
    return QIcon(pix)


class MainWindow(QMainWindow):
    def __init__(self, cfg: dict, engine: DictationEngine, signals: EngineSignals) -> None:
        super().__init__()
        self.cfg = cfg
        self.engine = engine
        self.signals = signals
        self._current_state = "paused"

        # Stel actieve taal in voor i18n
        set_language(self.cfg.get("ui_language", "en"))

        self.resize(600, 740)
        self._build_ui()
        self._build_tray()
        self._connect_signals()
        self._apply_translations()

        if self.cfg.get("auto_listen_on_start", False):
            self.engine.set_listening(True)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        top_bar = QHBoxLayout()
        self.toggle_btn = QPushButton()
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setMinimumHeight(40)
        self.toggle_btn.setStyleSheet("font-weight: bold; font-size: 14px;")
        self.toggle_btn.clicked.connect(self._on_toggle_clicked)

        self.state_badge = QLabel()
        self.state_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_badge.setMinimumWidth(140)
        top_bar.addWidget(self.toggle_btn, 1)
        top_bar.addWidget(self.state_badge)
        root.addLayout(top_bar)

        # Groep 1: Modellen, API & Vertaling
        self.grp_models = QGroupBox()
        self.form_models = QFormLayout(self.grp_models)

        url_row = QHBoxLayout()
        self.url_edit = QLineEdit(self.cfg["llm_url"])
        self.refresh_btn = QPushButton()
        self.refresh_btn.clicked.connect(self._on_refresh_models)
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(self.refresh_btn)
        self.lbl_api_url = QLabel()
        self.form_models.addRow(self.lbl_api_url, url_row)

        key_row = QHBoxLayout()
        self.api_key_edit = QLineEdit(self.cfg.get("api_key", ""))
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_key_chk = QCheckBox()
        self.show_key_chk.toggled.connect(
            lambda checked: self.api_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        key_row.addWidget(self.api_key_edit, 1)
        key_row.addWidget(self.show_key_chk)
        self.lbl_api_key = QLabel()
        self.form_models.addRow(self.lbl_api_key, key_row)

        whisper_row = QHBoxLayout()
        self.whisper_combo = QComboBox()
        self.whisper_combo.setEditable(True)
        self.whisper_combo.setCurrentText(self.cfg["whisper_model"])
        self.backend_combo = QComboBox()
        self.backend_combo.addItems(["rocm", "vulkan", "cpu"])
        self.backend_combo.setCurrentText(self.cfg["whisper_backend"])
        self.lbl_backend = QLabel()
        whisper_row.addWidget(self.whisper_combo, 1)
        whisper_row.addWidget(self.lbl_backend)
        whisper_row.addWidget(self.backend_combo)
        self.lbl_stt = QLabel()
        self.form_models.addRow(self.lbl_stt, whisper_row)

        lang_mode_row = QHBoxLayout()
        self.src_lang_combo = QComboBox()
        self.src_lang_combo.addItems(["nl", "en", "de", "fr"])
        self.src_lang_combo.setCurrentText(self.cfg.get("source_language", "nl"))

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("", "cleanup")
        self.mode_combo.addItem("", "translate_en")
        m_idx = self.mode_combo.findData(self.cfg.get("dictation_mode", "cleanup"))
        if m_idx >= 0:
            self.mode_combo.setCurrentIndex(m_idx)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self.lbl_lang_mode = QLabel()
        lang_mode_row.addWidget(self.src_lang_combo)
        lang_mode_row.addSpacing(10)
        lang_mode_row.addWidget(self.lbl_lang_mode)
        lang_mode_row.addWidget(self.mode_combo, 1)
        self.form_models.addRow(self.lbl_lang_mode, lang_mode_row)

        self.npu_polish_chk = QCheckBox()
        self.npu_polish_chk.setChecked(self.cfg["npu_polish_enabled"])
        self.form_models.addRow("", self.npu_polish_chk)

        self.llm_combo = QComboBox()
        self.llm_combo.setEditable(True)
        self.llm_combo.setCurrentText(self.cfg["llm_model"])
        self.lbl_llm = QLabel()
        self.form_models.addRow(self.lbl_llm, self.llm_combo)

        llm_params_row = QHBoxLayout()
        self.temp_spin = QDoubleSpinBox()
        self.temp_spin.setRange(0.0, 1.5)
        self.temp_spin.setSingleStep(0.05)
        self.temp_spin.setDecimals(2)
        self.temp_spin.setValue(float(self.cfg.get("llm_temperature", 0.0)))

        self.tokens_spin = QSpinBox()
        self.tokens_spin.setRange(32, 2048)
        self.tokens_spin.setSingleStep(64)
        self.tokens_spin.setValue(int(self.cfg.get("llm_max_tokens", 512)))

        self.sim_spin = QSpinBox()
        self.sim_spin.setRange(0, 100)
        self.sim_spin.setSingleStep(5)
        self.sim_spin.setSuffix("%")
        self.sim_spin.setValue(int(round(float(self.cfg.get("min_similarity", 0.62)) * 100)))

        self.lbl_temp = QLabel()
        self.lbl_tokens = QLabel()
        self.lbl_sim = QLabel()
        llm_params_row.addWidget(self.lbl_temp)
        llm_params_row.addWidget(self.temp_spin)
        llm_params_row.addSpacing(8)
        llm_params_row.addWidget(self.lbl_tokens)
        llm_params_row.addWidget(self.tokens_spin)
        llm_params_row.addSpacing(8)
        llm_params_row.addWidget(self.lbl_sim)
        llm_params_row.addWidget(self.sim_spin)
        llm_params_row.addStretch(1)
        self.lbl_llm_params = QLabel()
        self.form_models.addRow(self.lbl_llm_params, llm_params_row)

        self._cleanup_prompt_buf = self.cfg["system_prompt"]
        self._translate_prompt_buf = self.cfg.get("translate_prompt", DEFAULT_TRANSLATE_PROMPT)
        self.prompt_edit = QPlainTextEdit(
            self._translate_prompt_buf
            if self.cfg.get("dictation_mode") == "translate_en"
            else self._cleanup_prompt_buf
        )
        self.prompt_edit.setMaximumHeight(68)
        self.lbl_prompt = QLabel()
        self.form_models.addRow(self.lbl_prompt, self.prompt_edit)

        root.addWidget(self.grp_models)

        # Groep 2: Microfoon & Gevoeligheid
        self.grp_audio = QGroupBox()
        form_audio = QFormLayout(self.grp_audio)

        self.device_combo = QComboBox()
        self.device_combo.addItem("default")
        try:
            for dev in sd.query_devices():
                if dev["max_input_channels"] > 0 and "(hw:" not in dev["name"]:
                    if dev["name"] != "default":
                        self.device_combo.addItem(dev["name"])
        except Exception:
            pass
        self.device_combo.setCurrentText(self.cfg.get("input_device", "default"))
        self.lbl_mic = QLabel()
        form_audio.addRow(self.lbl_mic, self.device_combo)

        vu_row = QHBoxLayout()
        self.vu_bar = QProgressBar()
        self.vu_bar.setRange(0, 600)
        self.vu_bar.setTextVisible(False)
        self.vu_label = QLabel("RMS: 0")
        self.vu_label.setMinimumWidth(70)
        vu_row.addWidget(self.vu_bar, 1)
        vu_row.addWidget(self.vu_label)
        self.lbl_live_lvl = QLabel()
        form_audio.addRow(self.lbl_live_lvl, vu_row)

        rms_row = QHBoxLayout()
        self.rms_slider = QSlider(Qt.Orientation.Horizontal)
        self.rms_slider.setRange(10, 600)
        self.rms_slider.setValue(int(self.cfg["rms_threshold"]))
        self.rms_spin = QSpinBox()
        self.rms_spin.setRange(10, 2000)
        self.rms_spin.setValue(int(self.cfg["rms_threshold"]))
        self.rms_slider.valueChanged.connect(self.rms_spin.setValue)
        self.rms_spin.valueChanged.connect(self.rms_slider.setValue)
        rms_row.addWidget(self.rms_slider, 1)
        rms_row.addWidget(self.rms_spin)
        self.lbl_rms = QLabel()
        form_audio.addRow(self.lbl_rms, rms_row)

        vad_row = QHBoxLayout()
        self.vad_spin = QSpinBox()
        self.vad_spin.setRange(0, 3)
        self.vad_spin.setValue(int(self.cfg["vad_aggressiveness"]))
        self.trig_spin = QSpinBox()
        self.trig_spin.setRange(1, 15)
        self.trig_spin.setValue(int(self.cfg["trigger_frames"]))
        self.lbl_trig = QLabel()
        vad_row.addWidget(QLabel("VAD (0-3):"))
        vad_row.addWidget(self.vad_spin)
        vad_row.addSpacing(12)
        vad_row.addWidget(self.lbl_trig)
        vad_row.addWidget(self.trig_spin)
        vad_row.addStretch(1)
        self.lbl_filter = QLabel()
        form_audio.addRow(self.lbl_filter, vad_row)

        timing_row = QHBoxLayout()
        self.partial_spin = QDoubleSpinBox()
        self.partial_spin.setRange(0.3, 3.0)
        self.partial_spin.setSingleStep(0.05)
        self.partial_spin.setSuffix(" s")
        self.partial_spin.setValue(float(self.cfg["partial_interval_sec"]))

        self.silence_spin = QDoubleSpinBox()
        self.silence_spin.setRange(0.3, 3.0)
        self.silence_spin.setSingleStep(0.05)
        self.silence_spin.setSuffix(" s")
        self.silence_spin.setValue(float(self.cfg["silence_timeout_sec"]))

        self.lbl_part = QLabel()
        self.lbl_sil = QLabel()
        timing_row.addWidget(self.lbl_part)
        timing_row.addWidget(self.partial_spin)
        timing_row.addSpacing(12)
        timing_row.addWidget(self.lbl_sil)
        timing_row.addWidget(self.silence_spin)
        timing_row.addStretch(1)
        self.lbl_timing = QLabel()
        form_audio.addRow(self.lbl_timing, timing_row)

        root.addWidget(self.grp_audio)

        # Groep 3: Interface, Sneltoets & Keymap
        self.grp_sys = QGroupBox()
        form_sys = QFormLayout(self.grp_sys)

        ui_row = QHBoxLayout()
        self.ui_lang_combo = QComboBox()
        self.ui_lang_combo.addItem("English (EN)", "en")
        self.ui_lang_combo.addItem("Nederlands (NL)", "nl")
        l_idx = self.ui_lang_combo.findData(self.cfg.get("ui_language", "en"))
        if l_idx >= 0:
            self.ui_lang_combo.setCurrentIndex(l_idx)
        self.ui_lang_combo.currentIndexChanged.connect(self._on_ui_lang_changed)

        self.autostart_chk = QCheckBox()
        self.autostart_chk.setChecked(bool(self.cfg.get("auto_listen_on_start", False)))
        ui_row.addWidget(self.ui_lang_combo)
        ui_row.addSpacing(12)
        ui_row.addWidget(self.autostart_chk)
        ui_row.addStretch(1)
        self.lbl_ui_lang = QLabel()
        form_sys.addRow(self.lbl_ui_lang, ui_row)

        sc_row = QHBoxLayout()
        self.shortcut_edit = QKeySequenceEdit(QKeySequence(self.cfg["shortcut"]))
        self.apply_sc_btn = QPushButton()
        self.apply_sc_btn.clicked.connect(self._register_kde_shortcut)
        sc_row.addWidget(self.shortcut_edit, 1)
        sc_row.addWidget(self.apply_sc_btn)
        self.lbl_sc = QLabel()
        form_sys.addRow(self.lbl_sc, sc_row)

        xkb_row = QHBoxLayout()
        self.xkb_layout_edit = QLineEdit(self.cfg["xkb_layout"])
        self.xkb_layout_edit.setMaximumWidth(60)
        self.xkb_variant_edit = QLineEdit(self.cfg["xkb_variant"])
        self.xkb_variant_edit.setMaximumWidth(120)
        xkb_row.addWidget(QLabel("Layout:"))
        xkb_row.addWidget(self.xkb_layout_edit)
        xkb_row.addWidget(QLabel("Variant:"))
        xkb_row.addWidget(self.xkb_variant_edit)
        xkb_row.addStretch(1)
        self.lbl_keymap = QLabel()
        form_sys.addRow(self.lbl_keymap, xkb_row)

        root.addWidget(self.grp_sys)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(90)
        root.addWidget(self.log_box)

        bottom_bar = QHBoxLayout()
        self.status_lbl = QLabel()
        self.save_btn = QPushButton()
        self.save_btn.setStyleSheet("font-weight: bold;")
        self.save_btn.clicked.connect(self._save_and_apply)
        bottom_bar.addWidget(self.status_lbl, 1)
        bottom_bar.addWidget(self.save_btn)
        root.addLayout(bottom_bar)

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_tray_icon("paused"), self)
        menu = QMenu()
        self.tray_toggle_act = QAction(self)
        self.tray_toggle_act.triggered.connect(self.engine.toggle_listening)

        self.tray_show_act = QAction(self)
        self.tray_show_act.triggered.connect(self._show_window)

        self.tray_quit_act = QAction(self)
        self.tray_quit_act.triggered.connect(self._quit_app)

        menu.addAction(self.tray_toggle_act)
        menu.addAction(self.tray_show_act)
        menu.addSeparator()
        menu.addAction(self.tray_quit_act)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _apply_translations(self) -> None:
        self.setWindowTitle(tr("app_title"))
        self.grp_models.setTitle(tr("grp_models"))
        self.lbl_api_url.setText(tr("api_url"))
        self.refresh_btn.setText(tr("refresh_models"))
        self.lbl_api_key.setText(tr("api_key"))
        self.api_key_edit.setPlaceholderText(tr("api_key_ph"))
        self.show_key_chk.setText(tr("show_key"))
        self.lbl_stt.setText(tr("stt_model"))
        self.lbl_backend.setText(tr("backend"))
        self.lbl_lang_mode.setText(tr("lang_mode"))

        self.mode_combo.setItemText(0, tr("mode_cleanup"))
        self.mode_combo.setItemText(1, tr("mode_translate"))
        self.npu_polish_chk.setText(tr("npu_enable"))
        self.lbl_llm.setText(tr("llm_model"))
        self.lbl_llm_params.setText(tr("llm_params"))
        self.lbl_temp.setText(tr("temp"))
        self.lbl_tokens.setText(tr("tokens"))
        self.lbl_sim.setText(tr("sim"))

        is_trans = self.mode_combo.currentData() == "translate_en"
        self.lbl_prompt.setText(tr("prompt_translate" if is_trans else "prompt_cleanup"))
        self.sim_spin.setEnabled(not is_trans)

        self.grp_audio.setTitle(tr("grp_audio"))
        self.lbl_mic.setText(tr("mic"))
        self.lbl_live_lvl.setText(tr("live_lvl"))
        self.lbl_rms.setText(tr("rms"))
        self.lbl_filter.setText(tr("filter"))
        self.lbl_trig.setText(tr("start_frames"))
        self.lbl_timing.setText(tr("timing"))
        self.lbl_part.setText(tr("live_up"))
        self.lbl_sil.setText(tr("sil_pause"))

        self.grp_sys.setTitle(tr("grp_sys"))
        self.lbl_ui_lang.setText(tr("ui_lang"))
        self.autostart_chk.setText(tr("autostart"))
        self.lbl_sc.setText(tr("shortcut"))
        self.apply_sc_btn.setText(tr("bind_kde"))
        self.lbl_keymap.setText(tr("keymap"))

        self.log_box.setPlaceholderText(tr("log_ph"))
        self.status_lbl.setText(tr("ready"))
        self.save_btn.setText(tr("saved"))
        self.tray_show_act.setText(tr("tray_settings"))
        self.tray_quit_act.setText(tr("tray_quit"))
        self._on_state_changed(self._current_state)

    def _on_ui_lang_changed(self) -> None:
        lang = self.ui_lang_combo.currentData()
        self.cfg["ui_language"] = lang
        set_language(lang)
        self._apply_translations()

    def _on_mode_changed(self) -> None:
        new_mode = self.mode_combo.currentData()
        if new_mode == "translate_en":
            self._cleanup_prompt_buf = self.prompt_edit.toPlainText().strip()
            self.prompt_edit.setPlainText(self._translate_prompt_buf)
            self.sim_spin.setEnabled(False)
        else:
            self._translate_prompt_buf = self.prompt_edit.toPlainText().strip()
            self.prompt_edit.setPlainText(self._cleanup_prompt_buf)
            self.sim_spin.setEnabled(True)
        self._apply_translations()

    def _connect_signals(self) -> None:
        self.signals.rms_updated.connect(self._on_rms_updated)
        self.signals.state_changed.connect(self._on_state_changed)
        self.signals.transcript_logged.connect(self._on_transcript_logged)
        self.signals.status_message.connect(self.status_lbl.setText)
        self.signals.models_loaded.connect(self._on_models_loaded)

    @Slot(int)
    def _on_rms_updated(self, rms: int) -> None:
        self.vu_bar.setValue(min(600, rms))
        self.vu_label.setText(f"RMS: {rms}")
        thresh = self.rms_spin.value()
        color = "#2ecc71" if rms >= thresh else "#7f8c8d"
        self.vu_bar.setStyleSheet(f"QProgressBar::chunk {{ background-color: {color}; }}")

    @Slot(str)
    def _on_state_changed(self, state: str) -> None:
        self._current_state = state
        self.tray.setIcon(make_tray_icon(state))
        title = tr("app_title")
        if state == "paused":
            st_lbl = tr("state_paused")
            self.toggle_btn.setChecked(False)
            self.toggle_btn.setText(tr("start_listening"))
            self.tray_toggle_act.setText(tr("start_listening"))
            self.tray.setToolTip(f"{title} ({st_lbl})")
            self.state_badge.setText(st_lbl)
            self.state_badge.setStyleSheet(
                "background-color: #555; color: white; border-radius: 6px; padding: 6px; font-weight: bold;"
            )
        elif state == "listening":
            st_lbl = tr("state_listening")
            self.toggle_btn.setChecked(True)
            self.toggle_btn.setText(tr("pause_listening"))
            self.tray_toggle_act.setText(tr("pause_listening"))
            self.tray.setToolTip(f"{title} ({st_lbl})")
            self.state_badge.setText(st_lbl)
            self.state_badge.setStyleSheet(
                "background-color: #27ae60; color: white; border-radius: 6px; padding: 6px; font-weight: bold;"
            )
        elif state == "speaking":
            st_lbl = tr("state_speaking")
            self.tray.setToolTip(f"{title} ({st_lbl})")
            self.state_badge.setText(st_lbl)
            self.state_badge.setStyleSheet(
                "background-color: #d35400; color: white; border-radius: 6px; padding: 6px; font-weight: bold;"
            )

    @Slot(str, bool)
    def _on_transcript_logged(self, text: str, is_final: bool) -> None:
        if is_final:
            self.log_box.appendPlainText(f"✓ {text}")

    @Slot(list)
    def _on_models_loaded(self, models: list) -> None:
        cur_w = self.whisper_combo.currentText()
        cur_l = self.llm_combo.currentText()
        self.whisper_combo.clear()
        self.llm_combo.clear()
        for m in models:
            if "whisper" in m.lower() or "moonshine" in m.lower():
                self.whisper_combo.addItem(m)
            elif "kokoro" not in m.lower() and "ace" not in m.lower() and "think" not in m.lower():
                self.llm_combo.addItem(m)
        if cur_w:
            self.whisper_combo.setCurrentText(cur_w)
        if cur_l:
            self.llm_combo.setCurrentText(cur_l)

    def _on_toggle_clicked(self) -> None:
        self.engine.set_listening(self.toggle_btn.isChecked())

    def _on_refresh_models(self) -> None:
        self._save_and_apply()
        threading.Thread(target=self.engine.preload_models, daemon=True).start()

    def _save_and_apply(self) -> None:
        sc = self.shortcut_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        mode = self.mode_combo.currentData()
        if mode == "translate_en":
            self._translate_prompt_buf = self.prompt_edit.toPlainText().strip()
        else:
            self._cleanup_prompt_buf = self.prompt_edit.toPlainText().strip()

        self.cfg.update(
            {
                "ui_language": self.ui_lang_combo.currentData(),
                "llm_url": self.url_edit.text().strip(),
                "api_key": self.api_key_edit.text().strip(),
                "whisper_model": self.whisper_combo.currentText().strip(),
                "whisper_backend": self.backend_combo.currentText().strip(),
                "source_language": self.src_lang_combo.currentText().strip(),
                "dictation_mode": mode,
                "npu_polish_enabled": self.npu_polish_chk.isChecked(),
                "llm_model": self.llm_combo.currentText().strip(),
                "llm_temperature": round(self.temp_spin.value(), 2),
                "llm_max_tokens": self.tokens_spin.value(),
                "min_similarity": round(self.sim_spin.value() / 100.0, 2),
                "system_prompt": self._cleanup_prompt_buf,
                "translate_prompt": self._translate_prompt_buf,
                "rms_threshold": self.rms_spin.value(),
                "vad_aggressiveness": self.vad_spin.value(),
                "trigger_frames": self.trig_spin.value(),
                "partial_interval_sec": round(self.partial_spin.value(), 2),
                "silence_timeout_sec": round(self.silence_spin.value(), 2),
                "xkb_layout": self.xkb_layout_edit.text().strip(),
                "xkb_variant": self.xkb_variant_edit.text().strip(),
                "input_device": self.device_combo.currentText(),
                "shortcut": sc or "Ctrl+Shift+Space",
                "auto_listen_on_start": self.autostart_chk.isChecked(),
            }
        )
        save_config(self.cfg)
        self.engine.update_config(self.cfg)
        self.status_lbl.setText(tr("saved"))

    def _register_kde_shortcut(self) -> None:
        self._save_and_apply()
        sc = self.cfg["shortcut"]
        desktop_file = Path.home() / ".local/share/applications/wayland-dictate-toggle.desktop"
        toggle_bin = Path.home() / ".local/bin/wayland-dictate"

        desktop_file.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Wayland Live Dictate Toggle\n"
            f"Exec={toggle_bin} --toggle\n"
            "Icon=audio-input-microphone\n"
            "NoDisplay=true\n"
            "X-KDE-GlobalAccel-CommandShortcut=true\n",
            encoding="utf-8",
        )
        try:
            subprocess.run(
                [
                    "kwriteconfig6",
                    "--file",
                    "kglobalshortcutsrc",
                    "--group",
                    "services",
                    "--group",
                    "wayland-dictate-toggle.desktop",
                    "--key",
                    "_launch",
                    f"{sc},none,Wayland Live Dictate Toggle",
                ],
                check=False,
            )
            subprocess.run(["kbuildsycoca6"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(
                ["systemctl", "--user", "restart", "plasma-kglobalacceld.service"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.status_lbl.setText(f"KDE shortcut {sc} bound!")
            QMessageBox.information(
                self,
                "KDE Shortcut Bound",
                f"Shortcut {sc} has been registered in KDE Plasma.",
            )
        except Exception as exc:
            self.status_lbl.setText(f"Could not bind KDE shortcut: {exc}")

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.engine.toggle_listening()

    def _show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
        self.tray.showMessage(
            tr("app_title"),
            tr("tray_bg"),
            QSystemTrayIcon.MessageIcon.Information,
            1500,
        )

    def _quit_app(self) -> None:
        self.engine.shutdown()
        if SOCKET_PATH.exists():
            try:
                SOCKET_PATH.unlink()
            except Exception:
                pass
        QApplication.quit()


def start_ipc_server(engine: DictationEngine, window: MainWindow) -> None:
    if SOCKET_PATH.exists():
        try:
            SOCKET_PATH.unlink()
        except Exception:
            pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCKET_PATH))
    srv.listen(5)

    def serve() -> None:
        while not engine.stop_event.is_set():
            try:
                srv.settimeout(0.5)
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            with conn:
                cmd = conn.recv(64).decode("utf-8", errors="ignore").strip().upper()
                if cmd == "TOGGLE":
                    active = engine.toggle_listening()
                    conn.sendall(b"ON\n" if active else b"OFF\n")
                elif cmd == "ON":
                    engine.set_listening(True)
                    conn.sendall(b"ON\n")
                elif cmd == "OFF":
                    engine.set_listening(False)
                    conn.sendall(b"OFF\n")
                elif cmd == "SHOW":
                    QTimer.singleShot(0, window._show_window)
                    conn.sendall(b"OK\n")

    threading.Thread(target=serve, daemon=True).start()


def send_ipc_command(cmd: str) -> bool:
    if not SOCKET_PATH.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(str(SOCKET_PATH))
            sock.sendall(cmd.encode("utf-8"))
            resp = sock.recv(64).decode("utf-8").strip()
            print(resp)
            return True
    except Exception:
        return False


def main() -> None:
    if "--toggle" in sys.argv:
        if send_ipc_command("TOGGLE"):
            return
        cfg = load_config()
        cfg["auto_listen_on_start"] = True
        save_config(cfg)

    if "--show" in sys.argv and send_ipc_command("SHOW"):
        return

    if send_ipc_command("SHOW"):
        return

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setDesktopFileName("wayland-dictate")

    cfg = load_config()
    signals = EngineSignals()
    engine = DictationEngine(cfg, signals)
    window = MainWindow(cfg, engine, signals)
    start_ipc_server(engine, window)
    engine.start_threads()

    if "--minimized" not in sys.argv:
        window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
