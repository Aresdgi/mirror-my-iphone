# Security

Mirror my iPhone gives a Mac full control of an iPhone: it sees the screen, taps, types and opens apps. This document describes who should and shouldn't be able to use that, what this fork does about it, and what it doesn't protect against.

## What's worth protecting

- **Control of the iPhone** through WebDriverAgent (WDA): taps, typing, opening apps, reading the accessibility tree (which includes text on screen).
- **The iPhone's screen**: the USB video stream, WDA screenshots and WDA's MJPEG stream.
- **Code that runs on the Mac**, signed with your Apple ID and installed on the iPhone: the app, its Python dependencies, WebDriverAgent.
- **Personal data** in logs: device identifiers, the iPhone's name, the signing team, the certificate's email address, IP addresses.

## Threat model

| Who | Example | Status |
|---|---|---|
| Devices on the same network as the iPhone | Anyone on the café Wi-Fi | **Mitigated.** WDA listens only on the iPhone's loopback (`USE_IP=127.0.0.1`), its MJPEG stream too (`wda-patches/0001`). If WDA reports any other address, the app stops it. |
| Websites open in a browser on the Mac | A page sending requests to `127.0.0.1:8100` | **Mitigated.** No port on the Mac leads to WDA. The agent API (off by default) needs a token and refuses requests with an `Origin` header or another `Host`. |
| Other user accounts on the Mac | A second macOS user | **Mitigated** for the agent API (token file only readable by you). WDA is reachable only through usbmuxd; see "Not mitigated". |
| Other programs you run | A script or app that isn't malicious but shouldn't touch the phone | **Mostly mitigated.** No TCP port to WDA; the agent API is off unless you turn it on. |
| The original author or upstream changes | A new commit upstream | **Mitigated** by process: this fork doesn't pull automatically, has no Homebrew cask or auto-update, and the README shows how to review `upstream` before merging. |
| PyPI | A compromised or replaced package release | **Mitigated.** Every package, direct or indirect, is pinned to an exact version and SHA-256 (`requirements.lock`, `requirements-build.lock`) and installed with `--require-hashes --no-deps`. All but one are prebuilt wheels, so no install-time code runs; the exception (`hexdump` 3.3, source only, setup.py reviewed) is built with a hash-pinned setuptools without fetching anything else. |
| GitHub / the WDA repository | A moved tag or a modified repository | **Mitigated.** The app fetches one commit by hash, verifies it, applies its patch, and checks the commit and the SHA-256 of every changed file before each build. Only its own copy is ever built. |
| Root on the Mac | — | **Out of scope.** Root can do anything. This fork no longer runs anything as root (`tunneld` was removed). |
| Malware running as your user | A malicious program you installed | **Not mitigated**; see below. |

## Not mitigated

- **Malware running as your user.** It can do what you can:
  - open its own usbmux connection to WDA on the iPhone (`/var/run/usbmuxd` is open to every user by design on macOS), bypassing the app;
  - read `api.json` while agent control is on;
  - modify the app's Python environment in `~/Library/Application Support`, which isn't covered by the app's (ad-hoc) code signature. Code injected there runs inside the app and inherits its camera and microphone permissions, which cover the Mac's own camera too;
  - modify the app's WebDriverAgent copy (the next build refuses it, but malware could also change the app itself).

  Adding authentication inside WDA wouldn't help much: the token would have to reach WDA through xcodebuild's environment, which processes of the same user can read.
- **The agent API, when you turn it on**, gives any local program that can read your files full control of the iPhone, and AI agents act on what they see on screen, so text on the screen can try to steer them (prompt injection). Turn it on only while you use it.
- **The app is ad-hoc signed and not notarized.** Gatekeeper doesn't vouch for it; you're trusting your own build of code you reviewed.
- **pymobiledevice3 is large** (113 packages with its dependencies). Pinning stops silent changes, but the pinned versions themselves were not audited line by line.
- **WebDriverAgent itself** has no authentication and runs with UI Automation rights on the iPhone. Anyone with physical access to the unlocked Mac and the cable can control the phone while it runs.
- **Developer Mode and the developer certificate** stay enabled on the iPhone after you stop using the app. Turn Developer Mode off and delete WebDriverAgentRunner if you no longer need them.
- **Logs** are redacted by patterns and known values. Unusual identifiers in third-party output could still slip through. Log files are readable only by you.

## Reporting

This is a personal fork. Report problems with this fork's changes in the issues of [Aresdgi/mirror-my-iphone](https://github.com/Aresdgi/mirror-my-iphone); problems in the original app to [bhuwanadhikari/Mirror-my-iPhone](https://github.com/bhuwanadhikari/Mirror-my-iPhone); problems in WebDriverAgent or pymobiledevice3 to their projects.
