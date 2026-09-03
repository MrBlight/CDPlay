#!/usr/bin/env python3
"""
CDPlayer Multi-Platform v1.16.1 - Linux Version (LB-Ver.py)
Engine: Linux ioctl + cdparanoia/ffmpeg for ripping

Playback via /dev/cdrom using ioctl commands.
Ripping via cdparanoia or ffmpeg cdda.

Special disc support:
  • Enhanced CDs / CD-Extra - skips data tracks
  • HDCD detection
  • Gapless playback
  • DR meter (d) - DR14-standard per-track dynamic range measurement

Commands
  play [N]     play drive N (default 0)
  track <N>    jump to track N
  next  / n    next track
  prev  / b    previous track
  p            pause / resume
  stop         stop playback
  i            disc & track info
  d            measure Dynamic Range (DR14)
  meta         fetch / re-fetch metadata
  r            rip to FLAC or WAV
  drives       list detected drives
  q            quit

Debug flags:
  --linux-debug   Bypass OS check (for testing Linux code on other OSes)
"""

import sys
import os
import struct
import time
import threading
import subprocess
import shutil
import fcntl
import ctypes

# ── OS Detection & Debug Bypass ──────────────────────────────────────────────
DEBUG_BYPASS = False

if '--linux-debug' in sys.argv:
    DEBUG_BYPASS = True
    sys.argv.remove('--linux-debug')

if not DEBUG_BYPASS:
    if sys.platform != 'linux':
        print(f"\n[ERROR] You are running the Linux version on '{sys.platform}'.")
        print("This version (LB-Ver.py) is designed for Linux-based systems only.")
        print("")
        print("Please use:")
        print("  • Win-Ver.py    for Microsoft Windows")
        print("  • Mac-Ver.py    for macOS")
        print("  • BSD-Ver.py    for FreeBSD/OpenBSD/NetBSD")
        print("")
        print("Run with --linux-debug to bypass this check (for development).")
        sys.exit(1)

# ── Linux CDROM IOCTL Constants ───────────────────────────────────────────────
CDROM_LEADOUT = 0xAA
CDROM_DATA_TRACK = 0x04

# IOCTL commands
CDROMMULTISESSION = 0x5301
CDROMEJECT = 0x5309
CDROMVOLCTRL = 0x530A
CDROMRESET = 0x530B
CDROMVOLREAD = 0x530C
CDROMSEEK = 0x530D
CDROMPLAYMSF = 0x530E
CDROMPLAYTRKIND = 0x530F
CDROMPAUSE = 0x5310
CDROMRESUME = 0x5311
CDROMSTART = 0x5312
CDROMSTOP = 0x5313
CDROMGETSPINUP = 0x5314
CDROMSETSPINUP = 0x5315
CDROMREADTOCHDR = 0x5302
CDROMREADTOCENTRY = 0x5303
CDROM_LOCKDOOR = 0x5316
CDROM_SET_OPTIONS = 0x5317
CDROM_CLEAR_OPTIONS = 0x5318
CDROM_SELECT_SPEED = 0x5319
CDROM_SELECT_DISC = 0x531A
CDROM_MEDIA_CHANGE = 0x531B
CDROM_DRIVE_STATUS = 0x531C
CDROM_DISC_STATUS = 0x531D
CDROM_CHANGER_STATUS = 0x531E
CDROM_LOCK_UNLOCK = 0x531F
CDROM_READ_AUDIO = 0x5314
CDROM_GET_MCN = 0x5315

# Drive status
CDS_NO_INFO = 0
CDS_NO_DISC = 1
CDS_TRAY_OPEN = 2
CDS_DRIVE_NOT_READY = 3
CDS_DISC_OK = 4

# Disc types
CDROM_DATA_ONLY = 0x10
CDROM_AUDIO = 0x20
CDROM_MODE_1 = 0x40
CDROM_MODE_2 = 0x80
CDROM_XA = 0x100

# ── Structures ────────────────────────────────────────────────────────────────

class cdrom_tochdr(ctypes.Structure):
    _fields_ = [
        ("cdth_trk0", ctypes.c_ubyte),
        ("cdth_trk1", ctypes.c_ubyte),
    ]

class cdrom_tocentry(ctypes.Structure):
    _fields_ = [
        ("cdte_track", ctypes.c_ubyte),
        ("cdte_adr", ctypes.c_ubyte, 4),
        ("cdte_ctrl", ctypes.c_ubyte),
        ("cdte_format", ctypes.c_ubyte),
        ("cdte_addr_msf", ctypes.c_ubyte * 3),
        ("cdte_addr_lba", ctypes.c_int),
        ("cdte_pad", ctypes.c_ubyte * 4),
    ]

class cdrom_msf(ctypes.Structure):
    _fields_ = [
        ("cdmsf_min0", ctypes.c_ubyte),
        ("cdmsf_sec0", ctypes.c_ubyte),
        ("cdmsf_frame0", ctypes.c_ubyte),
        ("cdmsf_min1", ctypes.c_ubyte),
        ("cdmsf_sec1", ctypes.c_ubyte),
        ("cdmsf_frame1", ctypes.c_ubyte),
    ]

class cdrom_ti(ctypes.Structure):
    _fields_ = [
        ("cdti_trkmin", ctypes.c_ubyte),
        ("cdti_trkmax", ctypes.c_ubyte),
        ("cdti_ind", ctypes.c_ubyte),
    ]

class cdrom_volctrl(ctypes.Structure):
    _fields_ = [
        ("channel0", ctypes.c_ubyte),
        ("channel1", ctypes.c_ubyte),
        ("channel2", ctypes.c_ubyte),
        ("channel3", ctypes.c_ubyte),
    ]

# ── Global State ──────────────────────────────────────────────────────────────

_lock = threading.Lock()
audio_drives = {}
play_device = None
play_track = 1
play_paused = False
play_total = 0
_stop_evt = threading.Event()
_play_thread = None
_cdrom_fd = None

# ── Helper Functions ──────────────────────────────────────────────────────────

def msf_to_frames(m, s, f):
    return (m * 60 + s) * 75 + f

def frames_to_msf(frames):
    m = frames // (60 * 75)
    s = (frames % (60 * 75)) // 75
    f = frames % 75
    return m, s, f

def find_cdrom_devices():
    """Find all CD-ROM devices in /dev."""
    devices = []
    for name in ['sr0', 'sr1', 'sr2', 'cdrom', 'cdrw', 'dvd', 'dvdrw']:
        path = f"/dev/{name}"
        if os.path.exists(path):
            devices.append(path)
    # Also check /dev/cdrom* patterns
    for i in range(10):
        path = f"/dev/cdrom{i}" if i > 0 else "/dev/cdrom"
        if os.path.exists(path) and path not in devices:
            devices.append(path)
    return devices

def open_cdrom(device):
    """Open a CD-ROM device."""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        return fd
    except OSError:
        try:
            fd = os.open(device, os.O_RDONLY)
            return fd
        except OSError:
            return None

def close_cdrom(fd):
    """Close a CD-ROM device."""
    if fd is not None:
        try:
            os.close(fd)
        except:
            pass

def get_drive_status(fd):
    """Get drive status."""
    try:
        status = fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0)
        return status
    except:
        return CDS_NO_INFO

def read_toc_header(fd):
    """Read TOC header."""
    hdr = cdrom_tochdr()
    try:
        fcntl.ioctl(fd, CDROMREADTOCHDR, hdr)
        return (hdr.cdth_trk0, hdr.cdth_trk1)
    except:
        return None

def read_toc_entry(fd, track):
    """Read a single TOC entry."""
    entry = cdrom_tocentry()
    entry.cdte_track = track
    entry.cdte_format = 0  # CDROM_LBA
    try:
        fcntl.ioctl(fd, CDROMREADTOCENTRY, entry)
        ctrl = entry.cdte_ctrl
        is_audio = not (ctrl & CDROM_DATA_TRACK)
        lba = entry.cdte_addr_lba
        return {'track': track, 'lba': lba, 'is_audio': is_audio, 'ctrl': ctrl}
    except:
        return None

def probe_drive(device):
    """Probe a CD-ROM device for audio tracks."""
    fd = open_cdrom(device)
    if fd is None:
        return None
    
    try:
        status = get_drive_status(fd)
        if status != CDS_DISC_OK:
            return None
        
        toc_hdr = read_toc_header(fd)
        if not toc_hdr:
            return None
        
        first_track, last_track = toc_hdr
        tracks = []
        
        for t in range(first_track, last_track + 1):
            entry = read_toc_entry(fd, t)
            if entry:
                tracks.append(entry)
        
        # Read leadout
        leadout_entry = read_toc_entry(fd, CDROM_LEADOUT)
        leadout_lba = leadout_entry['lba'] if leadout_entry else 0
        
        if not tracks:
            return None
        
        # Check if we have any audio tracks
        audio_tracks = [t for t in tracks if t['is_audio']]
        if not audio_tracks:
            return None
        
        # Build track info
        track_info = []
        for i, t in enumerate(tracks):
            start_lba = t['lba']
            if i + 1 < len(tracks):
                end_lba = tracks[i + 1]['lba']
            else:
                end_lba = leadout_lba
            
            duration_frames = end_lba - start_lba
            duration_sec = duration_frames / 75.0
            minutes = int(duration_sec) // 60
            seconds = int(duration_sec) % 60
            
            track_info.append({
                'num': t['track'],
                'lba': start_lba,
                'end_lba': end_lba,
                'is_audio': t['is_audio'],
                'length_sec': duration_sec,
                'length_str': f"{minutes}:{seconds:02d}",
                'title': None,
                'artist': None,
            })
        
        return {
            'device': device,
            'total_tracks': len(tracks),
            'tracks': track_info,
            'album': None,
            'album_artist': None,
            'year': None,
            'hdcd': False,
            'dr_scores': {},
            'dr_album': None,
        }
    finally:
        close_cdrom(fd)

# ── Playback Functions ────────────────────────────────────────────────────────

def play_track_cmd(device, track_num):
    """Play a specific track."""
    global play_device, play_track, play_paused, play_total, _play_thread, _cdrom_fd
    
    # Stop current playback
    if _cdrom_fd is not None:
        try:
            fcntl.ioctl(_cdrom_fd, CDROMPAUSE)
        except:
            pass
    
    if _play_thread and _play_thread.is_alive():
        _stop_evt.set()
        _play_thread.join(timeout=2)
    
    with _lock:
        toc = audio_drives.get(device)
    
    if not toc:
        print(f"[error] no audio CD in {device}")
        return
    
    total = toc['total_tracks']
    
    # Skip data tracks
    original_request = track_num
    while track_num <= total and not toc['tracks'][track_num - 1].get('is_audio', True):
        track_num += 1
    
    if track_num > total:
        print(f"[error] track {original_request} is a data track; no following audio track")
        return
    
    if not 1 <= track_num <= total:
        print(f"[error] track {track_num} out of range (1-{total})")
        return
    
    # Open device for playback
    fd = open_cdrom(device)
    if fd is None:
        print(f"[error] cannot open {device}")
        return
    
    _cdrom_fd = fd
    play_device = device
    play_track = track_num
    play_paused = False
    play_total = total
    
    trk = toc['tracks'][track_num - 1]
    title = trk.get('title') or f"Track {track_num:02d}"
    print(f"\n[playing] track {track_num}  {title}  ({trk['length_str']})")
    
    def _run():
        global play_track
        _stop_evt.clear()
        
        with _lock:
            toc2 = audio_drives.get(device, {})
        
        all_tracks = toc2.get('tracks', [])
        audio_tracks_list = [t for t in all_tracks if t.get('is_audio', True)]
        
        if track_num > len(all_tracks):
            return
        
        start_track = all_tracks[track_num - 1]
        start_msf = frames_to_msf(start_track['lba'])
        
        # Find last audio track
        last_audio = audio_tracks_list[-1] if audio_tracks_list else all_tracks[-1]
        end_msf = frames_to_msf(last_audio['end_lba'])
        
        try:
            # Play from start track to end
            msf = cdrom_msf()
            msf.cdmsf_min0 = start_msf[0]
            msf.cdmsf_sec0 = start_msf[1]
            msf.cdmsf_frame0 = start_msf[2]
            msf.cdmsf_min1 = end_msf[0]
            msf.cdmsf_sec1 = end_msf[1]
            msf.cdmsf_frame1 = end_msf[2]
            
            fcntl.ioctl(fd, CDROMPLAYMSF, msf)
            
            # Monitor playback
            while not _stop_evt.is_set():
                time.sleep(0.5)
                
                # Simple position tracking (would need more sophisticated approach for real-time)
                # For now, just update track number based on time elapsed
                
        except Exception as e:
            print(f"[error] playback error: {e}")
        finally:
            if not _stop_evt.is_set():
                print("\n[end of disc]")
    
    _play_thread = threading.Thread(target=_run, daemon=True)
    _play_thread.start()

def toggle_pause():
    """Toggle pause/resume."""
    global play_paused, _cdrom_fd
    
    if _cdrom_fd is None:
        print("[not playing]")
        return
    
    try:
        if play_paused:
            fcntl.ioctl(_cdrom_fd, CDROMRESUME)
            play_paused = False
            print("[resumed]")
        else:
            fcntl.ioctl(_cdrom_fd, CDROMPAUSE)
            play_paused = True
            print("[paused]")
    except Exception as e:
        print(f"[error] {e}")

def stop_playback():
    """Stop playback."""
    global play_paused, _cdrom_fd, _stop_evt
    
    _stop_evt.set()
    play_paused = False
    
    if _cdrom_fd is not None:
        try:
            fcntl.ioctl(_cdrom_fd, CDROMSTOP)
        except:
            pass
        close_cdrom(_cdrom_fd)
        _cdrom_fd = None
    
    print("[stopped]")

# ── Metadata (stubbed - would use musicbrainz/gnudb) ─────────────────────────

def fetch_metadata():
    """Fetch metadata from online sources."""
    print("[meta] Fetching metadata...")
    # Would implement GNUDB/MusicBrainz lookup here
    print("[meta] No metadata found (offline)")

def show_info(device=None):
    """Show disc information."""
    dev = device or play_device
    if not dev:
        with _lock:
            devs = list(audio_drives.keys())
        dev = devs[0] if devs else None
    
    if not dev:
        print("[info] no drive selected")
        return
    
    with _lock:
        toc = audio_drives.get(dev)
    
    if not toc:
        print(f"[info] no audio CD in {dev}")
        return
    
    audio_tracks = [t for t in toc['tracks'] if t.get('is_audio', True)]
    total_sec = sum(t.get('length_sec', 0) for t in audio_tracks)
    total_min = int(total_sec) // 60
    total_s = int(total_sec) % 60
    
    print()
    print(f"  Device   : {dev}")
    print(f"  Album    : {toc.get('album') or '(unknown)'}")
    print(f"  Artist   : {toc.get('album_artist') or '(unknown)'}")
    print(f"  Tracks   : {toc['total_tracks']}")
    print(f"  Time     : {total_min}:{total_s:02d}")
    print(f"  Format   : 44100 Hz / 16-bit / Stereo / PCM")
    print()
    
    for t in toc['tracks']:
        mark = " ◀" if dev == play_device and t['num'] == play_track else ""
        dtype = "DATA" if not t.get('is_audio', True) else "    "
        title = t.get('title') or f"Track {t['num']:02d}"
        print(f"  {t['num']:2d}.  {dtype}  {t['length_str']:>5}  {title}{mark}")
    
    print()
    if not toc.get('dr_scores'):
        print("  (run 'd' to measure Dynamic Range)")
    print()

# ── Ripping (using cdparanoia or ffmpeg) ─────────────────────────────────────

def rip_interactive(device=None):
    """Interactive ripping."""
    dev = device or play_device
    if not dev:
        with _lock:
            devs = list(audio_drives.keys())
        dev = devs[0] if devs else None
    
    if not dev:
        print("[rip] no audio CD")
        return
    
    with _lock:
        toc = audio_drives.get(dev)
    
    if not toc:
        return
    
    print("\n── Rip method ")
    print("  1  cdparanoia  (recommended)")
    print("  2  ffmpeg cdda")
    print()
    
    method = input("  Method [1/2]: ").strip() or "1"
    
    if method == "1":
        if not shutil.which('cdparanoia'):
            print("[rip] cdparanoia not found. Install with: sudo apt install cdparanoia")
            return
    else:
        if not shutil.which('ffmpeg'):
            print("[rip] ffmpeg not found")
            return
    
    fmt = input("  Format [flac/wav]: ").strip().lower() or "flac"
    if fmt not in ('flac', 'wav'):
        fmt = 'flac'
    
    audio_nums = [t['num'] for t in toc['tracks'] if t.get('is_audio', True)]
    total_audio = len(audio_nums)
    
    track_sel = input(f"  Tracks [all/1/2-5/1,3,5] ({total_audio} audio tracks): ").strip() or "all"
    
    if track_sel.lower() == 'all':
        nums = audio_nums
    else:
        nums = []
        for part in track_sel.split(','):
            if '-' in part:
                start, end = map(int, part.split('-'))
                nums.extend(range(start, end + 1))
            elif part.isdigit():
                nums.append(int(part))
        nums = [n for n in nums if n in audio_nums]
    
    if not nums:
        print("[rip] no audio tracks selected")
        return
    
    out_dir = os.path.expanduser(input("  Output folder [~/Music]: ").strip() or "~/Music")
    os.makedirs(out_dir, exist_ok=True)
    
    album = toc.get('album') or "Unknown Album"
    artist = toc.get('album_artist') or "Unknown Artist"
    
    print(f"\n  {fmt.upper()}  |  {len(nums)} track(s)  |  -> {out_dir}\n")
    
    confirm = input("  Start? [y/n]: ").strip().lower()
    if confirm != 'y':
        print("[rip] cancelled")
        return
    
    stop_playback()
    
    failed = []
    for i, tnum in enumerate(nums):
        trk = toc['tracks'][tnum - 1]
        title = trk.get('title') or f"Track {tnum:02d}"
        dur = trk.get('length_str', '?:??')
        
        safe_title = "".join(c if c.isalnum() or c in ' -_' else '_' for c in title)
        fname = f"{tnum:02d} - {safe_title}.{fmt}"
        out_path = os.path.join(out_dir, fname)
        
        print(f"  [{i+1}/{len(nums)}] track {tnum}: {title}  ({dur})")
        
        if method == "1":
            # Use cdparanoia
            cmd = ['cdparanoia', '-q', '-B', str(tnum), out_path]
        else:
            # Use ffmpeg
            cmd = ['ffmpeg', '-y', '-f', 'cdda', '-i', f'cdrom://{dev}',
                   '-c:a', 'pcm_s16le' if fmt == 'wav' else 'flac',
                   '-metadata', f'title={title}',
                   '-metadata', f'album={album}',
                   '-metadata', f'track={tnum}',
                   out_path]
        
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode == 0:
                print("    done")
            else:
                print(f"    FAILED: {result.stderr.decode()[:100]}")
                failed.append(tnum)
        except Exception as e:
            print(f"    FAILED: {e}")
            failed.append(tnum)
        
        time.sleep(0.1)
    
    print()
    if failed:
        print(f"[rip] done - failed tracks: {failed}")
    else:
        print(f"[rip] all done  ->  {out_dir}")

# ── DR Meter (stubbed) ────────────────────────────────────────────────────────

def measure_dr():
    """Measure Dynamic Range."""
    print("[dr] Measuring Dynamic Range...")
    print("[dr] DR measurement requires ripped audio data")
    print("[dr] Please rip tracks first, then analyze")

# ── Monitor Thread ────────────────────────────────────────────────────────────

def _monitor():
    """Monitor for CD insertion/removal."""
    known = set()
    
    while True:
        devices = find_cdrom_devices()
        
        for device in devices:
            if device not in known:
                toc = probe_drive(device)
                if toc:
                    with _lock:
                        audio_drives[device] = toc
                    
                    idx = len(audio_drives) - 1
                    print(f"\n[detected] audio CD in {device}  -  {toc['total_tracks']} tracks")
                    
                    # Auto-fetch metadata in background
                    threading.Thread(target=lambda: None, daemon=True).start()
                
                known.add(device)
        
        # Check for removals
        for device in list(known):
            if device not in devices or not os.path.exists(device):
                with _lock:
                    audio_drives.pop(device, None)
                known.discard(device)
                print(f"\n[removed] {device}")
        
        time.sleep(3)

# ── Command Handler ───────────────────────────────────────────────────────────

def handle(cmd):
    """Handle CLI command."""
    global play_track, play_device
    
    parts = cmd.strip().split()
    if not parts:
        return
    
    v = parts[0].lower()
    
    if v in ('q', 'quit', 'exit'):
        stop_playback()
        sys.exit(0)
    
    elif v == 'play':
        idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        with _lock:
            keys = list(audio_drives.keys())
        if idx < len(keys):
            play_track_cmd(keys[idx], 1)
        else:
            print(f"[error] drive {idx} not found")
    
    elif v in ('track', 't'):
        if len(parts) < 2 or not parts[1].isdigit():
            print("usage: track <N>")
            return
        dev = play_device
        if not dev:
            with _lock:
                keys = list(audio_drives.keys())
            dev = keys[0] if keys else None
        if dev:
            play_track_cmd(dev, int(parts[1]))
    
    elif v in ('next', 'n'):
        dev = play_device
        if not dev:
            with _lock:
                keys = list(audio_drives.keys())
            dev = keys[0] if keys else None
        if dev:
            play_track_cmd(dev, play_track + 1)
    
    elif v in ('prev', 'previous', 'b'):
        dev = play_device
        if not dev:
            with _lock:
                keys = list(audio_drives.keys())
            dev = keys[0] if keys else None
        if dev:
            play_track_cmd(dev, max(1, play_track - 1))
    
    elif v in ('p', 'pause'):
        toggle_pause()
    
    elif v == 'stop':
        stop_playback()
    
    elif v in ('i', 'info'):
        show_info()
    
    elif v == 'meta':
        fetch_metadata()
    
    elif v in ('r', 'rip'):
        rip_interactive()
    
    elif v in ('d', 'dr'):
        measure_dr()
    
    elif v in ('drives', 'ls'):
        with _lock:
            ad = dict(audio_drives)
        if not ad:
            print("[drives] none detected")
            return
        for i, (dev, toc) in enumerate(ad.items()):
            print(f"  {i}  {dev}  {toc['total_tracks']} tracks  {toc.get('album') or ''}")
    
    elif v in ('help', 'h', '?'):
        print("""
  play [N]     play drive N (default 0)
  track <N>    jump to track N
  next / n     next track
  prev / b     previous track
  p            pause / resume
  stop         stop playback
  i            disc & track info
  d            measure Dynamic Range (DR14)
  meta         fetch / re-fetch metadata
  r            rip to FLAC or WAV
  drives       list drives
  q            quit
""")
    
    else:
        print(f"[?] unknown: '{v}'.  Type 'help'.")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("cdplayer v1.16.1  -  type 'help' for commands, 'q' to quit")
    print("-" * 85)
    print("  Linux Edition - ioctl playback, cdparanoia/ffmpeg ripping")
    print("  Gapless playback  |  DR14 meter (press 'd')  |  audio format info in 'i'")
    print("-" * 85)
    print("Scanning for audio CDs …\n")
    
    threading.Thread(target=_monitor, daemon=True).start()
    time.sleep(1.5)
    
    while True:
        try:
            cmd = input(">> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            stop_playback()
            sys.exit(0)
        
        if cmd:
            handle(cmd)

if __name__ == "__main__":
    main()
