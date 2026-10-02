# iPhone Mirror

See and control your iPhone screen from your Mac — an open-source alternative to Apple's iPhone Mirroring, which is unavailable in the EU due to the Digital Markets Act (DMA).

## Features

- **Screen Mirroring** — Live iPhone screen on your Mac via USB (up to 60 FPS)
- **Touch Control** — Tap, swipe, long press, and scroll via mouse
- **Hardware Buttons** — Home, Lock, Volume Up/Down from the toolbar
- **Auto-Connect** — Detects your iPhone automatically when plugged in
- **Battery Status** — Shows current battery level in the status bar
- **Dark UI** — Native dark theme

## Requirements

- macOS with Xcode Command Line Tools
- Python 3.10+
- iPhone with **Developer Mode** enabled (Settings > Privacy & Security > Developer Mode)
- USB cable
- iOS 17+: `tunneld` must be running (see below)

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/iPhoneMirroring.git
cd iPhoneMirroring

# 2. Set up (creates venv + installs dependencies)
bash setup.sh

# 3. Start the developer tunnel (iOS 17+, requires sudo)
sudo .venv/bin/python3 -m pymobiledevice3 remote tunneld

# 4. Run the app (in a new terminal)
.venv/bin/python3 main.py
```

## Touch Control Setup (Optional)

Screen mirroring works immediately. For touch control, you also need [WebDriverAgent](https://github.com/appium/WebDriverAgent):

1. Clone WDA into the project directory:
   ```bash
   git clone https://github.com/appium/WebDriverAgent.git
   ```
2. Open `WebDriverAgent/WebDriverAgent.xcodeproj` in Xcode
3. Set your Development Team in the project settings (Signing & Capabilities)
4. Build and run the `WebDriverAgentRunner` scheme on your iPhone once via Xcode (Product > Test)

After the initial Xcode setup, the app will auto-start WDA on each connection.

## How It Works

| Component | Technology |
|---|---|
| Screen Capture | AVFoundation USB screen stream (as in QuickTime); fallback: DVT Screenshot Service |
| Touch Input | WebDriverAgent HTTP API (port 8100) |
| USB Communication | pymobiledevice3 (usbmux + lockdown) |
| GUI | PyQt6 |

The screen arrives as a USB video stream at up to 60 FPS, the same way QuickTime Player records an iPhone. While it runs, iOS shows a clean status bar (09:41, full battery) and may route the iPhone's audio to the Mac, where the app plays it. If the stream isn't available (e.g. pyobjc is missing), the app falls back to the DVT Screenshot Service, captured over 4 channels in parallel (~20 FPS). Touch events are translated from mouse coordinates to iPhone screen points and sent to WebDriverAgent.

## Project Structure

```
iPhoneMirroring/
├── main.py              # Entry point
├── main_window.py       # PyQt6 GUI (window, toolbar, status bar)
├── device_manager.py    # iPhone discovery, DVT connection, WDA launch
├── screen_capture.py    # Capture thread (USB video stream, DVT fallback)
├── video_stream.py      # USB video stream via AVFoundation
├── input_handler.py     # Mouse-to-touch translation, WDA HTTP client
├── setup.sh             # Setup script
├── requirements.txt     # Python dependencies
└── WebDriverAgent/      # Appium WDA (git submodule)
```

## Troubleshooting

**"Developer-Tunnel wird benoetigt"**
Start tunneld: `sudo .venv/bin/python3 -m pymobiledevice3 remote tunneld`

**Low FPS**
The status bar shows the capture mode. `DVT` means the USB video stream wasn't available — check the log and that the pyobjc packages from `requirements.txt` are installed. With `USB-Video`, FPS drops to 0 while the iPhone display is off.

**Touch not working**
Make sure WDA is installed on the device (see Touch Control Setup). The yellow banner disappears once WDA connects.

**"Trust This Computer" dialog**
Tap "Trust" on your iPhone and re-run the app.

## License

MIT
