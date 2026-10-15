# Windows VR Notifier

Pipes Windows toast notifications directly into SteamVR, so you never miss a Discord message, email, or system alert while in VR.

Notifications appear as a floating card in your headset — fully positioned, opacity-controlled, and fade-animated. Includes a desktop control panel **and** an in-VR dashboard tab with clickable settings.

---

## Features

### Core notification mirroring
- Listens to the Windows notification listener (`UserNotificationListener`) via the `winsdk` WinRT projection
- Renders each toast as a styled card and uploads it to a SteamVR overlay
- Queue-based display — one notification at a time, with a "+N more" indicator when backlog grows
- Word-wrapped body text (up to 3 lines) instead of hard truncation

### In-VR dashboard tab
- Appears as a clickable button in the SteamVR dashboard
- **Tabbed interface** with four sections:
  - **Home** — DND toggle + Send Test Notification buttons
  - **Position** — X/Y/Z +/- buttons (VR-friendly, no keyboard needed)
  - **Appearance** — Width/Alpha/Display/Fade +/- buttons
  - **History** — recent notifications list
- All clicks handled via controller laser pointer
- Changes apply live and sync back to the desktop control panel

### Desktop control panel
- System tray app with optional window
- **Tabbed interface** mirroring the VR dashboard:
  - **Controls** — DND checkbox, Test, Preview buttons
  - **Position** — X/Y/Z sliders for fine control
  - **Appearance** — Width, Alpha, Display duration, Fade duration sliders
  - **History** — scrollable notification log
- **Preview Overlay** button — shows a static card in VR for adjusting position/alpha live
- Status indicator (connecting / running / DND active / error)
- Settings saved to `config.json` automatically

### SteamVR integration
- Auto-generates and registers the `.vrmanifest` on first launch
- Auto-enables **auto-launch** with SteamVR — starts automatically when SteamVR starts
- No manual manifest editing required

### Reliability
- Background-thread SteamVR init (window never freezes during startup)
- Auto-restarting listener with exponential backoff if the event loop crashes
- WinRT collection access via index (not iteration) — avoids the `iter() returned non-iterator` crash
- Timeouts on all WinRT async calls — no more event-loop deadlocks
- Alternating PNG filenames for dashboard renders — no file corruption from concurrent read/write
- Debounced dashboard rendering — rapid clicks won't crash SteamVR's overlay subsystem
- Thread-safe DND flag with proper Tk marshaling

---

## Requirements

- **Windows 10 or later** (uses WinRT notification APIs)
- **SteamVR installed** and a VR headset connected
- **Notification listener access** granted in Windows Settings

---

## Installation

1. Download the latest `WN2SVR.exe` from [Releases](../../releases)
2. Place it anywhere on your PC
3. Run it once — it will auto-register with SteamVR
4. Grant notification access (see below)
5. Done — it will auto-start with SteamVR from now on

### Granting notification access (one-time)

1. Open **Windows Settings**
2. Go to **Privacy & security → Notifications**
3. Enable **"Allow apps to access notifications"**

If you skip this step, the app will run but never receive any notifications.

---

## Usage

### Basic operation

1. Start SteamVR
2. WN2SVR starts automatically (if auto-launch is enabled)
3. Put on your headset
4. When a Windows notification arrives, it appears as a card in VR

### Accessing the in-VR settings

1. Press the system button on your controller to open the SteamVR dashboard
2. Find the **VR headset icon** in the button list at the bottom
3. Click it — the settings panel appears
4. Use the tabs and +/- buttons to adjust everything without leaving VR

### Desktop control panel

- Click the tray icon to show/hide the window
- Use the tabs to adjust position and appearance
- Click **"Preview Overlay"** to see a static card in VR while you drag sliders
- Right-click the tray icon for quick actions (Open, Quit)

### DND mode

- **Toggle from VR:** Click the DND button on the Home tab
- **Toggle from desktop:** Check the DND checkbox on the Controls tab
- When active, notifications are still logged to history but not shown in VR

---

## Configuration

All settings are stored in `config.json` next to the executable:

| Setting | Description | Default |
|---------|-------------|---------|
| `pos_x` | Overlay X position (-1.0 to 1.0) | 0.25 |
| `pos_y` | Overlay Y position (-1.0 to 1.0) | 0.20 |
| `pos_z` | Overlay Z distance (-2.0 to -0.1) | -0.75 |
| `overlay_width_m` | Overlay width in meters (0.1 to 1.5) | 0.45 |
| `overlay_alpha` | Overlay opacity (0.1 to 1.0) | 0.9 |
| `display_seconds` | Seconds each notification stays visible (1.0 to 15.0) | 5.0 |
| `fade_duration` | Fade in/out duration in seconds (0.0 to 1.0) | 0.25 |
| `dnd_enabled` | Do Not Disturb flag | false |
| `history_max` | Max notifications kept in history | 50 |

You can edit this file manually (while the app is closed) or use the GUI.

---

## How it works

WN2SVR runs as a background process that hooks into two Windows subsystems:

- **Windows Notification Listener** — polls for new toast notifications via the WinRT `UserNotificationListener` API
- **SteamVR Overlay API** — renders each notification as a texture and displays it on a floating card in VR

It also registers a **dashboard overlay** with SteamVR, which gives you an in-VR settings panel with tabbed controls (no need to take off the headset to tweak things).

The desktop control panel and system tray icon provide an alternative way to configure everything from your PC.

---

## Troubleshooting

### "SteamVR error" in status
SteamVR isn't running or the headset isn't connected. Start SteamVR and restart the app.

### No notifications appearing
Grant notification access in Windows Settings (see Installation above).

### Dashboard tab doesn't appear in SteamVR
Restart SteamVR once. The tab should appear after that. If not, run the app once from the desktop to re-register the manifest.

### Overlay stuck visible / dashboard vanishes
This was a bug in earlier versions caused by file corruption from concurrent read/write. It's fixed — if you're still seeing it, make sure you're on the latest version.

### Tray icon shows as a random color
This was a bug in earlier versions (RGB vs RGBA mode for the icon). It's fixed in the latest version.

---

## License

All rights reserved. This software is provided as a compiled executable for end-user use.

Redistribution, modification, reverse engineering, or redistribution of the source code is not permitted without explicit written permission from the author.

---

## Acknowledgments

Built using:
- [SteamVR](https://store.steampowered.com/steamvr) — Valve's VR runtime
- [Pillow](https://python-pillow.org) — Image rendering
- Windows Runtime APIs for notification access

---

## Roadmap

Potential future features (not implemented):
- Text-to-speech read-aloud via Windows SAPI
- Per-app blocklist/allowlist
- Auto-DND when headset is taken off
- Customizable card themes
- Notification grouping for rapid-fire messages
- System tray quick actions
- Run at Windows startup