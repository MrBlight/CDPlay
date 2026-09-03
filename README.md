# CDPlayer Multi-Platform v1.16.1

Terminal CD audio player and ripper for Windows and Linux

Please note that this program only supports Windows and Linux, ignore refferences to other OS's if present as failed implenentations have been removed
## Version History

- **v1.14** - Original Windows-only version with MCI playback and SCSI ripping (see CDPlayer_14_.py for legacy windows version)
- **v1.15** - Bug fixes for pause/stop functionality and CLI display glitches
- **v1.16.1** - Multi-platform release with separate OS versions and PATH installation

## Quick Start

### Windows
```cmd
python Win-Ver.py
```

### Linux
```bash
python3 LB-Ver.py
```

## Installation (Add to PATH)

### Windows
```cmd
setup-windows.bat
```
This creates `cdplay` and `uncdplayer` commands in your PATH.

### Linux
```bash
chmod +x setup-unix.sh
./setup-unix.sh
```
This creates `cdplay` and `uncdplayer` commands in `~/.local/bin`.

After installation:
- Run `cdplay` from anywhere to launch the appropriate version for your OS
- Run `uncdplayer` to uninstall

## Features

### Playback
- Gapless CD playback
- Track navigation (next, previous, jump to track)
- Pause/resume functionality
- Stop playback

### Ripping
- **Windows**: SCSI READ CD (recommended) or ffmpeg cdda
- **Linux**: cdparanoia (recommended) or ffmpeg cdda
- Output formats: FLAC, WAV (16/24/32-bit on Windows)
- Automatic metadata embedding

### Special Disc Support
- Enhanced CDs / CD-Extra (QuickTime multimedia)
- HDCD detection (20-bit peak-extension encoding)
- FIM UltraHD / 32-bit mastered CDs
- Mixed-mode CDs (data + audio tracks)
- Pre-emphasis track detection
- Quadraphonic (4-channel) audio support

### Audio Analysis
- DR14 Dynamic Range metering
- Per-track DR scores
- Album-level DR rating
- Format information (sample rate, bit depth, channels)

### Metadata
- GNUDB/freedb lookup
- MusicBrainz integration
- Automatic album art tagging (via ffmpeg)

## Commands

| Command | Description |
|---------|-------------|
| `play [N]` | Play drive N (default 0) |
| `track <N>` | Jump to track N |
| `next` / `n` | Next track |
| `prev` / `b` | Previous track |
| `p` | Pause / resume |
| `stop` | Stop playback |
| `i` | Disc & track info |
| `d` | Measure Dynamic Range (DR14) |
| `meta` | Fetch/re-fetch metadata |
| `r` | Rip to FLAC or WAV |
| `drives` | List detected drives |
| `q` | Quit |

## Debug Flags

For development and cross-platform testing:

```bash
# Windows version on non-Windows OS
python Win-Ver.py --win-debug

# Linux version on non-Linux OS
python LB-Ver.py --linux-debug
```

These flags bypass OS detection to allow testing platform-specific code.

## System Requirements

### All Platforms
- Python 3.9 or later
- CD-ROM drive

### Windows
- Windows XP through 11 (NT kernel required)
- Administrator privileges for SCSI operations

### Linux
- Kernel CD-ROM support (standard in all distributions)
- Optional: `cdparanoia` for high-quality ripping
- Optional: `ffmpeg` for FLAC encoding

## Architecture

This multi-platform edition uses separate Python files for each OS:

- **Win-Ver.py** - Windows implementation using MCI and SCSI passthrough
- **LB-Ver.py** - Linux implementation using ioctl and cdparanoia

Each version includes OS detection that silently checks the platform and displays a helpful message if run on the wrong OS.

## Troubleshooting

### "No drives detected"
- Ensure a CD is inserted
- Check that your user has permissions to access the CD drive
- On Linux, try: `sudo usermod -aG cdrom $USER` then log out/in

### "Cannot open device"
- Windows: Run as Administrator for SCSI access
- Linux: Check group membership (cdrom group)

### Ripping fails
- Install dependencies:
  - Linux: `sudo apt install cdparanoia ffmpeg`

### Python not found
- Install Python 3.9+ from https://python.org
- Make sure "Add Python to PATH" is checked during installation (Windows)

## License

See LICENSE file for terms.

## Credits

I hope you will enjoy gapless and high-fidelity CD playback.
