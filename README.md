# Soul-Calibur-Windows-Builder

A Python tool that builds a working Windows version of the
[SoulCalibur II static recompilation (RingOut)](https://github.com/jackpoison-prog/RingOut)
— and produces a runnable `.exe`.

Two ways to run it:

- **GUI** — `ringout_windows_gui.py` — a graphical interface with file
  pickers, options, and a live log panel. **Recommended for most users.**
- **CLI** — `ringout_windows_builder.py` — the command-line builder the GUI
  drives under the hood.

## Quick start (GUI)

```bat
python ringout_windows_gui.py
```

1. Click **Browse...** next to *Disc Image* and pick your `.iso`.
2. (Optional) pick an output folder, or leave it for the default.
3. Click **Start Build**.
4. Watch the log — it stays visible and is also saved to
   `logs/build_TIMESTAMP.log` (plus `logs/latest.log`).

If the build fails, a dialog shows the log file path so you never lose
the error message.

## Quick start (CLI)

```bat
python ringout_windows_builder.py --iso "C:\games\SoulCalibur2.iso"

:: With logging to a file:
python ringout_windows_builder.py --iso "C:\games\SoulCalibur2.iso" --log build.log
```

## What it does

`ringout_windows_builder.py` automates the **entire** build pipeline:

1. **Downloads & installs** every required build tool (Git, CMake, Ninja,
   clang via llvm-mingw, Python 3) — no manual setup needed.
2. **Clones** the RingOut repository (everything is vendored, no submodule init).
3. **Builds the ModernGekko runtime** → `moderngekko-run.exe`
4. **Builds the DolRecomp recompiler** → `dolrecomp.exe`
5. **Compiles the native Windows launcher** → `RingOut.exe`
6. **Assembles** a complete, self-contained package under `RingOut-windows\`,
   bundling the toolchain so the game module can be recompiled from your own
   disc later without installing anything else.
7. **Optionally recompiles your disc** if you pass `--iso path\to\game.iso`.

The end result is a folder you can double-click `RingOut.exe` in to play.

## Requirements

- Windows 10 / 11, x86-64
- Internet access on first run (to download the toolchain)
- ~6 GB free disk space
- A Vulkan-capable GPU with up-to-date drivers
- A GameCube disc image of SoulCalibur II that **you already own**

## Usage

```bat
:: Build everything and recompile your disc in one shot:
python ringout_windows_builder.py --iso "C:\games\SoulCalibur2.iso"

:: Or just build the runtime (RingOut.exe will ask for a disc on first launch):
python ringout_windows_builder.py

:: If you already have all tools on PATH:
python ringout_windows_builder.py --skip-deps --iso game.iso
```

### All flags

| Flag | Description |
|---|---|
| `--iso PATH` | Path to your SC2 GameCube disc image (`.iso` / `.wbfs`) |
| `--out DIR` | Output directory (default: `RingOut-windows` beside the script) |
| `--skip-deps` | Assume all build tools are on PATH; skip downloads |
| `--rebuild` | Delete cached build directories and rebuild from scratch |
| `--no-launcher` | Skip building the native `RingOut.exe` launcher |
| `--jobs N` | Parallel compile jobs (default: CPU count) |
| `--log FILE` | Also write all output to a log file (plain text, no colour codes) |

### GUI options

The GUI (`ringout_windows_gui.py`) provides the same options as checkboxes
and fields. It automatically:

- Saves every build to `logs/build_YYYY-MM-DD_HH-MM-SS.log`
- Maintains `logs/latest.log` as a copy of the most recent build
- Passes `--log` to the builder as a second safety net
- Shows a colour-coded, scrollable log panel that never closes
- Displays a dialog with the log file path on success or failure

## In-game controls

| Key | Action |
|---|---|
| Escape | Settings menu |
| Alt+Enter | Fullscreen |
| Alt+W | Widescreen (16:9) toggle |
| F1–F8 | Load state |
| Shift+F1–F8 | Save state |
| Shift+Escape | Quit |

## How it works under the hood

RingOut is a **static recompilation** of SoulCalibur II: the GameCube disc's
PowerPC executable is translated ahead-of-time into C, compiled for x86-64,
and run as native code inside a Dolphin-derived runtime (ModernGekko) that
provides graphics, audio, input, and hardware emulation.

No game data or game code is distributed. You supply a disc image you already
own; the tool extracts it and recompiles the executable on your machine.

## Credits

This builder is a thin automation layer on top of other people's work:

- **Dolphin Emulator Project** — the runtime is Dolphin (GPL-2.0-or-later)
- **ExpansionPak / ModernGekko** — the recomp runtime chassis
- **ExpansionPak / DolRecomp** — the PowerPC → C static recompiler (GPL-3.0-or-later)
- **Dear ImGui** — settings overlay UI (MIT)
- **Jack Poison** — the RingOut project this automates
- **Bandai Namco Entertainment** — the original game (not included, not redistributed)

SoulCalibur II is a trademark of Bandai Namco Entertainment. This is an
unofficial fan project, not affiliated with or endorsed by any of the above.

## License

The builder script itself is released under the MIT License. The components
it downloads and builds retain their respective licenses (GPL-2.0 / GPL-3.0).
