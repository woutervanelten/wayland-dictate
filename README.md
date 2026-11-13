# Wayland Live Dictate

**Wayland Live Dictate** is a lightning-fast, privacy-centric, real-time live speech-to-text dictation and translation tool built specifically for Linux desktop environments running **Wayland**. 

It connects to any local or remote OpenAI-compatible API backend (such as Lemonade AI, vLLM, LM Studio, or LiteLLM), processes audio streams using Silero/WebRTC VAD (Voice Activity Detection), cleans up hesitations and filler words, and types the text directly into any active window using **`dotool`** (kernel-level `/dev/uinput` simulation without X11/XWayland dependencies).

---

## Key Features

* **Wayland Native (`dotool`)**: Works flawlessly across modern Wayland compositors (like KDE Plasma 6 and GNOME) without needing X11.
* **Universal API Support**: Compatible with any OpenAI-compatible `/audio/transcriptions` and `/chat/completions` endpoint.
* **Dual Operating Modes**:
  * *Clean up*: Retains your spoken language (e.g., Dutch) while intelligently stripping filler words (uhm, eh, etc.) and fixing self-corrections.
  * *Live Translate to English*: Instantly translates spoken input into fluent, natural English on the fly.
* **Smart Voice Activity Detection (VAD)**: Real-time RMS metering and WebRTC VAD ensure accurate utterance boundaries and zero accidental triggering.
* **Bilingual UI (EN / NL)**: Fully internationalized with clean JSON-based localization and instant switching.
* **System Tray & Global Shortcuts**: Runs discreetly in the system tray with global hotkey support (e.g., via KDE Plasma global shortcuts).

---

## Installation

### Option 1: Run the AppImage (Recommended)
Download the pre-compiled **AppImage** from the [Releases](https://github.com/YOUR_GITHUB_USERNAME/wayland-dictate/releases) page, make it executable, and run it:

```bash
chmod +x Wayland-Live-Dictate-x86_64.AppImage
./Wayland-Live-Dictate-x86_64.AppImage
```

### Option 2: Run from Source (Python / UV)
Ensure you have dotool installed on your system, then clone and run the repository:
```bash
git clone [https://github.com/YOUR_GITHUB_USERNAME/wayland-dictate.git](https://github.com/YOUR_GITHUB_USERNAME/wayland-dictate.git)
cd wayland-dictate

# Create a virtual environment and install dependencies
uv venv .venv
source .venv/bin/activate
uv pip install -e .

# Run the application
wayland-dictate
```
Note on dotool: Ensure the dotool daemon (dotoold) is running and your user has proper permissions to /dev/uinput.

## Configuration

Settings are automatically saved to ~/.config/wayland-dictate/config.json. You can configure:
* API Endpoint URL (e.g., http://localhost:13305/api/v1)
* API Key (optional, for secured endpoints or cloud gateways)
* STT & LLM Models (e.g., Whisper-Large-v3-Turbo, Gemma)
* VAD & RMS Noise Thresholds
* Keyboard Layout & XKB Variants

## AppImage & Distribution

To build a fresh AppImage locally (requires uv and pyinstaller):
```bash
./packaging/build_appimage.sh
```
