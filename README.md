# iPhone Mirror

See and control your iPhone screen from your Mac — an open-source alternative to Apple's iPhone Mirroring, which is unavailable in the EU due to the Digital Markets Act (DMA).

## Features

- **Screen Mirroring** — Live iPhone screen on your Mac via USB (up to 60 FPS)
- **Touch Control** — Click, long press, swipe and scroll with the mouse or trackpad
- **Hardware Buttons** — Home, Lock, Volume Up/Down from the toolbar and the Device menu
- **Doctor** — Checks the iPhone and the Mac on first launch and explains how to fix what's missing
- **Developer Logs** — Live log of the app, pymobiledevice3 and WebDriverAgent in the sidebar
- **Real-size Window** — The phone is shown at its actual size in points (or the largest step that fits)
- **Auto-Connect** — Detects your iPhone automatically when plugged in
- **Battery Status** — Shows current battery level in the status bar

## Install

```bash
brew tap bhuwanadhikari/iphone-mirror https://github.com/bhuwanadhikari/iPhoneMirroring
brew install --cask iphone-mirror
```

This puts **iPhone Mirror** in `/Applications`, so it shows up in Spotlight and Launchpad (the Apps view on macOS 26), and adds the `iphone-mirror` command. Homebrew also installs `python@3.12`, which the app runs on; its packages go into a private environment next to the cask. Update with `brew upgrade --cask iphone-mirror`; `brew uninstall --zap --cask iphone-mirror` also removes settings, logs and the downloaded WebDriverAgent.

## First launch: the doctor

On first launch the sidebar opens on the **Doctor** tab and checks everything mirroring and touch control need, with a fix for each problem (some fixes are a button). Run it again any time from Help › Run Doctor, or in a terminal:

```bash
iphone-mirror doctor
```

| | Needed for | How |
|---|---|---|
| iPhone connected via USB, Mac trusted | Mirroring | Data cable; tap **Trust** on the iPhone |
| Camera access for iPhone Mirror | Mirroring | macOS treats the iPhone's screen stream like a camera and asks once |
| Developer Mode on the iPhone | Touch control | Settings › Privacy & Security › Developer Mode (the doctor can reveal the option) |
| Xcode and an Apple ID in Xcode | Touch control | App Store; Xcode › Settings › Accounts (a free Apple ID works) |
| WebDriverAgent | Touch control | The doctor downloads it; the app builds and starts it on the iPhone |
| Developer trusted, UI Automation on | Touch control | Settings › General › VPN & Device Management › Trust; Settings › Developer › Enable UI Automation |
| Developer tunnel (`tunneld`) | Optional: screenshot fallback on iOS 17+ | The doctor starts it (asks for your password), or `iphone-mirror tunnel` |

## Controls

| Mac | iPhone |
|---|---|
| Click | Tap |
| Click and hold, or right-click | Long press |
| Drag | Swipe along the same path |
| Scroll wheel / two-finger scroll | Swipe in the scroll direction |
| ⇧⌘H / ⌘L | Home / Lock |
| ⌘0 / ⌘+ / ⌘− | Actual size / larger / smaller window |
| ⌃⌘S | Show or hide the sidebar (Doctor, Settings, Logs) |

The window has a fixed size: the phone is drawn at a zoom of its real size in points (100 % = an iPhone 15's 393 × 852 pt), at the largest step that fits your screen. A single, constant scale factor keeps the picture sharp and the per-frame scaling cheap; 75 % is exactly half or a quarter of a 3x iPhone's pixels on Retina and 1x displays.

Touch goes through [WebDriverAgent](https://github.com/appium/WebDriverAgent), which can only send whole gestures: a drag is replayed on the iPhone when you release the mouse, sped up but with the same speed at the moment you let go. There is no double tap: a double click is two separate taps, which arrive too far apart for iOS to count as one double tap.

## Run from source

```bash
git clone https://github.com/bhuwanadhikari/iPhoneMirroring.git
cd iPhoneMirroring
bash setup.sh                 # creates .venv and installs the dependencies
.venv/bin/python3 main.py     # the app
.venv/bin/python3 cli.py doctor
```

## How It Works

| Component | Technology |
|---|---|
| Screen Capture | AVFoundation USB screen stream (as in QuickTime); fallback: DVT Screenshot Service |
| Touch Input | WebDriverAgent HTTP API (port 8100), W3C actions for all gestures |
| USB Communication | pymobiledevice3 (usbmux + lockdown) |
| GUI | PyQt6 |

The screen arrives as a USB video stream at up to 60 FPS, the same way QuickTime Player records an iPhone. While it runs, iOS shows a clean status bar (09:41, full battery) and may route the iPhone's audio to the Mac, where the app plays it. If the stream isn't available (e.g. pyobjc is missing), the app falls back to the DVT Screenshot Service, captured over 4 channels in parallel (~20 FPS); on iOS 17+ that needs the developer tunnel. Touch events are translated from mouse coordinates to iPhone screen points and sent to WebDriverAgent, which the app builds and runs with `xcodebuild`, signed with the first Apple ID signed in to Xcode.

## Packaging and releases

`packaging/build_app.sh` builds `dist/iPhone Mirror.app` and the zip the cask downloads. The bundle contains a small native launcher (`packaging/launcher.c`), the Python sources and the icon. The launcher runs Python inside the app's own process, so the Dock, the menu bar and permission prompts say "iPhone Mirror" rather than "Python". Python packages live outside the bundle, in a venv that `bootstrap.sh` creates: in `$(brew --prefix)/Caskroom/iphone-mirror/<version>/venv` for Homebrew installs (the cask runs it after installing), otherwise in `~/Library/Application Support/iPhone Mirror/venv` (the app sets it up in Terminal on first launch).

To publish a version: bump `__version__` in `version.py`, commit, then run `packaging/release.sh`. It builds the zip, points `Casks/iphone-mirror.rb` at it, pushes, and creates the GitHub release (needs the [GitHub CLI](https://cli.github.com)). The repository is its own Homebrew tap, so `brew upgrade` sees the new version right away. The app is ad-hoc signed, not notarized; the cask clears the quarantine flag so macOS opens it.

## Project Structure

```
iPhoneMirroring/
├── main.py              # Entry point (logging, theme)
├── main_window.py       # Window: phone view, sidebar, menus, zoom
├── doctor.py            # Prerequisite checks and fixes (no UI)
├── doctor_panel.py      # Doctor tab
├── log_panel.py         # Logs tab and in-memory log buffer
├── settings_panel.py    # Settings tab (preview, not wired up yet)
├── cli.py               # iphone-mirror command (doctor, tunnel, logs)
├── device_manager.py    # iPhone discovery, DVT connection, WDA launch
├── wda_project.py       # Finding, downloading and signing WebDriverAgent
├── screen_capture.py    # Capture thread (USB video stream, DVT fallback)
├── video_stream.py      # USB video stream via AVFoundation
├── input_handler.py     # Mouse-to-touch gestures, WDA HTTP client
├── paths.py, version.py # Shared locations and the app version
├── assets/AppIcon.png   # App icon (drawn by packaging/make_icon.py)
├── packaging/           # .app launcher, Info.plist, build and release scripts
├── Casks/               # Homebrew cask (this repo doubles as the tap)
├── setup.sh             # Setup for running from source
└── WebDriverAgent/      # Appium WDA (git submodule, optional)
```

## Troubleshooting

Start with the Doctor tab or `iphone-mirror doctor`. The Logs tab (or `iphone-mirror logs`, or `~/Library/Logs/iPhone Mirror`) shows what the app, pymobiledevice3 and xcodebuild are doing.

**Low FPS**
The status bar shows the capture mode. `screenshots` means the USB video stream wasn't available. With `USB video`, FPS drops to 0 while the iPhone display is off.

**Touch not working**
The banner above the phone says why; the Doctor tab has the details. The first WebDriverAgent build takes a few minutes, and after the first install you need to trust the developer on the iPhone (Settings › General › VPN & Device Management).

**"Trust This Computer" dialog**
Tap "Trust" on your iPhone; the app connects by itself.

## License

MIT
