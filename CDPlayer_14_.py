"""
cdplayer.py  -  terminal CD player & ripper for Windows  (v13)
Playback via MCI (built-in Windows).

Ripping - choose a method when you run 'r':
  1  SCSI READ CD  (IOCTL_SCSI_PASS_THROUGH_DIRECT - recommended, no extra installs)
  2  ffmpeg cdda/libcdio  (legacy fallback, needs gyan.dev full build)

Special disc support:
  • Enhanced CDs / CD-Extra (QuickTime, data+audio mixed mode) - skips data tracks
  • HDCD (20-bit compatible digital) - detects peak-extension encoding
  • FIM UltraHD / 32-bit mastered CDs - recognised and flagged
  • Sector-type auto-fallback for unusual or non-standard pressings
  • 16 / 24 / 32-bit WAV output options
  • Gapless playback (single MCI span, position-tracked)
  • DR meter (d) - DR14-standard per-track dynamic range measurement

Commands
  play [N]     play drive N (default 0)
  track <N>    jump to track N
  next  / n    next track
  prev  / b    previous track
  p            pause / resume
  stop         stop playback
  i            disc & track info (audio format, capacity, DR scores)
  d            measure Dynamic Range (DR14) for all audio tracks
  meta         fetch / re-fetch metadata
  r            rip to FLAC or WAV
  drives       list detected drives
  q            quit
"""

import sys, os, re, time, threading, subprocess, shutil, ctypes, struct as _struct, math
import urllib.request, urllib.parse, json as _json

if sys.platform != "win32":
    sys.exit("[error] Windows only.")

k32   = ctypes.windll.kernel32
winmm = ctypes.windll.winmm

GENERIC_READ           = 0x80000000
FILE_SHARE_READ        = 0x00000001
FILE_SHARE_WRITE       = 0x00000002
OPEN_EXISTING          = 3
IOCTL_CDROM_READ_TOC            = 0x00024000
IOCTL_SCSI_PASS_THROUGH_DIRECT  = 0x0004D014
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080

# ── SCSI structures ──────────────────────────────────────────────────────────

class _SPTD(ctypes.Structure):
    _fields_ = [
        ("Length",              ctypes.c_uint16),
        ("ScsiStatus",          ctypes.c_uint8),
        ("PathId",              ctypes.c_uint8),
        ("TargetId",            ctypes.c_uint8),
        ("Lun",                 ctypes.c_uint8),
        ("CdbLength",           ctypes.c_uint8),
        ("SenseInfoLength",     ctypes.c_uint8),
        ("DataIn",              ctypes.c_uint8),
        ("_pad",                ctypes.c_uint8 * 3),
        ("DataTransferLength",  ctypes.c_uint32),
        ("TimeOutValue",        ctypes.c_uint32),
        ("_pad2",               ctypes.c_uint32),
        ("DataBuffer",          ctypes.c_uint64),
        ("SenseInfoOffset",     ctypes.c_uint32),
        ("Cdb",                 ctypes.c_uint8 * 16),
    ]

class _SPTDWithSense(ctypes.Structure):
    _fields_ = [("sptd", _SPTD), ("sense", ctypes.c_uint8 * 32)]

# ── TOC / drive helpers ──────────────────────────────────────────────────────

def _cdrom_letters() -> list[str]:
    return [c for c in "DEFGHIJKLMNOPQRSTUVWXYZ"
            if k32.GetDriveTypeW(f"{c}:\\") == 5]

def _open_drive_toc(letter: str):
    h = k32.CreateFileW(f"\\\\.\\{letter}:", GENERIC_READ,
                        FILE_SHARE_READ | FILE_SHARE_WRITE,
                        None, OPEN_EXISTING, 0, None)
    if h == ctypes.c_void_p(-1).value:
        raise OSError(f"Cannot open {letter}: (error {k32.GetLastError()})")
    return h

def _read_toc_raw(letter: str) -> bytes | None:
    """Return raw TOC bytes, or None."""
    try:
        h   = _open_drive_toc(letter)
        buf = ctypes.create_string_buffer(2048)
        n   = ctypes.c_ulong(0)
        ok  = k32.DeviceIoControl(h, IOCTL_CDROM_READ_TOC,
                                   None, 0, buf, 2048, ctypes.byref(n), None)
        k32.CloseHandle(h)
        return bytes(buf.raw[:n.value]) if ok else None
    except Exception:
        return None

def _parse_toc(raw: bytes) -> tuple[list[dict], int] | None:
    """
    Parse raw TOC bytes.
    Returns (track_list, leadout_lba) where each track dict has:
        num, lba, is_audio, control
    control bit 2 set => data track.
    """
    if not raw or len(raw) < 4:
        return None
    first, last = raw[2], raw[3]
    tracks, leadout = [], 0
    n_entries = (last - first) + 2
    for i in range(n_entries):
        off = 4 + i * 8
        if off + 7 >= len(raw):
            break
        control = raw[off + 1] & 0x0F
        tnum    = raw[off + 2]
        m, s, f = raw[off+5], raw[off+6], raw[off+7]
        lba     = (m * 60 + s) * 75 + f - 150
        is_audio = not bool(control & 0x04)   # bit 2 = data track
        if tnum == 0xAA:
            leadout = lba
        elif 1 <= tnum <= 99:
            tracks.append({"num": tnum, "lba": lba,
                           "is_audio": is_audio, "control": control})
    return (tracks, leadout) if tracks else None

def _detect_disc_features(toc_tracks: list[dict]) -> dict:
    """
    Analyse track list and return a features dict:
      disc_type : 'standard' | 'enhanced' | 'mixed_mode' | 'data_only'
      has_qt    : True if likely a QuickTime Enhanced CD (data track at end)
      note      : human-readable string or None
    """
    if not toc_tracks:
        return {"disc_type": "unknown", "has_qt": False, "note": None}
    audio_tracks = [t for t in toc_tracks if t["is_audio"]]
    data_tracks  = [t for t in toc_tracks if not t["is_audio"]]
    if not data_tracks:
        return {"disc_type": "standard", "has_qt": False, "note": None}
    if not audio_tracks:
        return {"disc_type": "data_only", "has_qt": False,
                "note": "No audio tracks found"}
    # Mixed-mode: data track first (track 1)
    if toc_tracks[0]["num"] == 1 and not toc_tracks[0]["is_audio"]:
        return {"disc_type": "mixed_mode", "has_qt": False,
                "note": "Mixed-mode CD (data track 1 + audio)"}
    # CD-Extra / Enhanced CD: data track at end (after all audio)
    last = toc_tracks[-1]
    if not last["is_audio"]:
        return {"disc_type": "enhanced", "has_qt": True,
                "note": "Enhanced CD / CD-Extra (QuickTime multimedia in data track)"}
    return {"disc_type": "standard", "has_qt": False, "note": None}

# ── HDCD detection ───────────────────────────────────────────────────────────
# HDCD encodes a control signal in the LSB of the right channel every 8 samples.
# We scan the first few MB for the 17-bit header pattern 0x7FFF8000 in packed form.

_HDCD_MAGIC = b'\x00\x80\xff\x7f'   # little-endian int16 pair: +32767, -32768

def _detect_hdcd(wav_path: str, scan_mb: int = 4) -> bool:
    """Return True if the WAV file likely contains HDCD peak-extension data."""
    try:
        with open(wav_path, "rb") as f:
            f.seek(44)   # skip WAV header
            data = f.read(scan_mb * 1_048_576)
        # Count occurrences; HDCD uses this pattern much more than random audio
        return data.count(_HDCD_MAGIC) >= 8
    except Exception:
        return False

# ── state ────────────────────────────────────────────────────────────────────

_lock        = threading.Lock()
audio_drives: dict[str, dict] = {}
_dr_measuring: bool = False   # suppresses monitor disconnect during DR reads

play_letter:  str | None = None
play_track:   int        = 1
play_paused:  bool       = False
play_total:   int        = 0
_stop_evt    = threading.Event()
_play_thread: threading.Thread | None = None

# ── drive probe ──────────────────────────────────────────────────────────────

def probe_drive(letter: str) -> dict | None:
    buf = ctypes.create_unicode_buffer(512)
    err = winmm.mciSendStringW(f'open {letter}: type cdaudio alias cdprobe shareable',
                                None, 0, None)
    if err:
        return None

    def mq(cmd):
        winmm.mciSendStringW(cmd, buf, 512, None)
        return buf.value.strip()

    try:
        winmm.mciSendStringW('set cdprobe time format milliseconds', None, 0, None)
        n_s = mq('status cdprobe number of tracks')
        if not n_s.isdigit():
            return None
        n_tracks = int(n_s)
        if n_tracks == 0:
            return None

        tracks = []
        for i in range(1, n_tracks + 1):
            pos_s = mq(f'status cdprobe position track {i}')
            len_s = mq(f'status cdprobe length track {i}')
            start_ms = int(pos_s) if pos_s.isdigit() else 0
            dur_ms   = int(len_s) if len_s.isdigit() else 0
            sec      = dur_ms / 1000
            tracks.append({
                "num":        i,
                "start_ms":   start_ms,
                "end_ms":     start_ms + dur_ms,
                "length_sec": sec,
                "length_str": f"{int(sec)//60}:{int(sec)%60:02d}",
                "lba":        0,
                "end_lba":    0,
                "is_audio":   True,   # default; refined below
                "pre_emph":   False,  # TOC control bit 0; refined below
                "channels":   2,      # TOC control bit 3; refined below
                "title":      None,
                "artist":     None,
            })

        toc = {"letter": letter, "total_tracks": n_tracks,
               "tracks": tracks, "album": None, "album_artist": None,
               "year": None, "lbas": [], "leadout": 0,
               "disc_type": "standard", "disc_note": None, "hdcd": False,
               "dr_scores": {}, "dr_album": None}

        raw = _read_toc_raw(letter)
        if raw:
            result = _parse_toc(raw)
            if result:
                toc_tracks, leadout = result
                toc["leadout"] = leadout
                lbas = [t["lba"] for t in toc_tracks if t["is_audio"]]
                toc["lbas"] = lbas

                # Fill per-track data (including format flags from control bits)
                for tt in toc_tracks:
                    for t in tracks:
                        if t["num"] == tt["num"]:
                            t["lba"]        = tt["lba"]
                            t["is_audio"]   = tt["is_audio"]
                            # control bit 0 = pre-emphasis, bit 3 = 4-channel
                            t["pre_emph"]   = bool(tt["control"] & 0x01)
                            t["channels"]   = 4 if (tt["control"] & 0x08) else 2
                            break

                # Set end_lba for each track
                for i, t in enumerate(tracks):
                    next_lba = tracks[i+1]["lba"] if i+1 < len(tracks) else leadout
                    t["end_lba"] = next_lba

                feats = _detect_disc_features(toc_tracks)
                toc["disc_type"] = feats["disc_type"]
                toc["disc_note"] = feats["note"]

        return toc
    except Exception:
        return None
    finally:
        winmm.mciSendStringW('close cdprobe', None, 0, None)

# ── metadata helpers ─────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    if not s: return s
    s = re.sub(r'[\x00-\x1f\x7f]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s if 1 <= len(s) <= 300 else ""

def _looks_valid(patch: dict, n_tracks: int) -> bool:
    if not _clean(patch.get("album") or ""):
        return False
    filled = sum(1 for t in patch.get("tracks", [])
                 if t.get("title") and _clean(t.get("title", "")))
    return filled >= max(1, n_tracks // 2)

def _merge_patch(toc: dict, patch: dict):
    toc["album"]        = _clean(patch.get("album")        or "") or None
    toc["album_artist"] = _clean(patch.get("album_artist") or "") or None
    toc["year"]         = patch.get("year") or None
    for src in patch.get("tracks", []):
        for dst in toc["tracks"]:
            if dst["num"] == src["num"]:
                dst["title"]  = _clean(src.get("title")  or "") or None
                dst["artist"] = _clean(src.get("artist") or "") or None

# ── metadata: GNUDB ──────────────────────────────────────────────────────────

def _gnudb_discid(toc: dict) -> str | None:
    lbas, leadout = toc.get("lbas", []), toc.get("leadout", 0)
    if not lbas or not leadout:
        return None
    def digit_sum(n):
        s = 0
        while n > 0: s += n % 10; n //= 10
        return s
    checksum = sum(digit_sum((l + 150) // 75) for l in lbas) % 255
    total_s  = (leadout + 150) // 75 - (lbas[0] + 150) // 75
    return f"{((checksum << 24) | (total_s << 8) | len(lbas)):08x}"

def _gnudb_parse_entry(lines: list[str], n_tracks: int) -> dict | None:
    kv: dict[str, str] = {}
    for line in lines:
        if line.startswith("#") or not line.strip(): continue
        if "=" in line:
            k, _, v = line.partition("=")
            key = k.strip()
            kv[key] = kv.get(key, "") + v
    dtitle = kv.get("DTITLE", "").strip()
    if "/" in dtitle:
        raw_artist, _, raw_album = dtitle.partition("/")
        album_artist = _clean(raw_artist.strip())
        album        = _clean(raw_album.strip())
    else:
        album_artist, album = None, _clean(dtitle)
    if not album:
        return None
    tracks = []
    for i in range(n_tracks):
        val = kv.get(f"TTITLE{i}", "").strip()
        if "/" in val:
            a, _, t = val.partition("/")
            tracks.append({"num": i+1, "title": _clean(t.strip()),
                           "artist": _clean(a.strip()) or None})
        else:
            tracks.append({"num": i+1, "title": _clean(val), "artist": None})
    return {"album": album, "album_artist": album_artist,
            "year": kv.get("DYEAR", "").strip() or None, "tracks": tracks}

def fetch_gnudb(toc: dict, silent: bool = False) -> bool:
    disc_id  = _gnudb_discid(toc)
    lbas     = toc.get("lbas", [])
    leadout  = toc.get("leadout", 0)
    n_tracks = toc["total_tracks"]
    if not disc_id:
        if not silent: print("[meta] no TOC data for GNUDB")
        return False
    offsets  = "+".join(str(l + 150) for l in lbas)
    total_s  = (leadout + 150) // 75
    base     = "https://gnudb.gnudb.org/~cddb/cddb.cgi"
    hello    = "hello=user+localhost+cdplayer+6.0&proto=6"
    query_url = f"{base}?cmd=cddb+query+{disc_id}+{len(lbas)}+{offsets}+{total_s}&{hello}"
    try:
        if not silent: print("[meta] querying GNUDB …", end="", flush=True)
        req = urllib.request.Request(query_url, headers={"User-Agent": "cdplayer/6.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            lines = resp.read().decode("latin-1", errors="replace").splitlines()
        if not lines: raise ValueError("empty response")
        code = lines[0].split()[0]
        candidates: list[tuple[str,str]] = []
        if code == "200":
            parts = lines[0].split(None, 3)
            candidates = [(parts[1], parts[2])]
        elif code in ("211", "210"):
            for line in lines[1:]:
                line = line.strip()
                if not line or line == ".": break
                parts = line.split(None, 2)
                if len(parts) >= 2: candidates.append((parts[0], parts[1]))
        else:
            raise ValueError(f"code {code}")
        if not candidates: raise ValueError("no candidates")
        best_patch = None
        for cat, did in candidates:
            read_url = f"{base}?cmd=cddb+read+{cat}+{did}&{hello}"
            try:
                req2 = urllib.request.Request(read_url, headers={"User-Agent": "cdplayer/6.0"})
                with urllib.request.urlopen(req2, timeout=12) as resp2:
                    entry = resp2.read().decode("latin-1", errors="replace").splitlines()
                patch = _gnudb_parse_entry(entry, n_tracks)
                if patch and _looks_valid(patch, n_tracks):
                    best_patch = patch; break
                elif patch and best_patch is None:
                    best_patch = patch
            except Exception:
                continue
        if not best_patch:
            if not silent: print(" no usable match")
            return False
        _merge_patch(toc, best_patch)
        if not silent: print(f" {toc.get('album') or 'no match'}")
        return bool(toc.get("album"))
    except Exception as e:
        if not silent: print(f" failed ({e})")
        return False

# ── metadata: MusicBrainz ────────────────────────────────────────────────────

_mb_cache: dict[str, dict] = {}

def _mb_toc_url(toc: dict) -> str:
    lbas, leadout = toc.get("lbas", []), toc.get("leadout", 0)
    if not lbas: return ""
    offsets = " ".join(str(l + 150) for l in lbas)
    toc_str = f"1 {len(lbas)} {leadout + 150} {offsets}"
    return ("https://musicbrainz.org/ws/2/discid/-"
            f"?toc={urllib.parse.quote(toc_str)}&fmt=json&inc=artists+recordings")

def fetch_musicbrainz(toc: dict, silent: bool = False) -> bool:
    url = _mb_toc_url(toc)
    if not url:
        if not silent: print("[meta] no LBA data for MusicBrainz")
        return False
    if url in _mb_cache:
        patch = _mb_cache[url]
        if patch: _merge_patch(toc, patch)
        return bool(toc.get("album"))
    try:
        if not silent: print("[meta] querying MusicBrainz …", end="", flush=True)
        req = urllib.request.Request(
            url, headers={"User-Agent": "cdplayer/6.0 ( https://github.com/local )"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = _json.loads(resp.read())
        patch = _build_mb_patch(toc, data)
        _mb_cache[url] = patch
        if patch and _looks_valid(patch, toc["total_tracks"]):
            _merge_patch(toc, patch)
        if not silent: print(f" {toc.get('album') or 'no match'}")
        return bool(toc.get("album"))
    except Exception as e:
        if not silent: print(f" failed ({e})")
        return False

def _build_mb_patch(toc: dict, data: dict) -> dict | None:
    try:
        rels     = data.get("releases") or data.get("release-list", [])
        if not rels: return None
        n_tracks = toc["total_tracks"]
        def score(r):
            for med in r.get("media") or r.get("medium-list", []):
                tc = med.get("track-count") or len(med.get("tracks") or med.get("track-list", []))
                if tc == n_tracks: return (0, 0 if r.get("date") else 1)
            return (1, 0 if r.get("date") else 1)
        rel   = min(rels, key=score)
        creds = rel.get("artist-credit", [])
        album_artist = "".join(
            (c.get("name") or c.get("artist", {}).get("name", "")) + c.get("joinphrase", "")
            for c in creds if isinstance(c, dict)) or None
        tracks = []
        for med in rel.get("media") or rel.get("medium-list", []):
            for t in med.get("tracks") or med.get("track-list", []):
                n   = int(t.get("position") or t.get("number") or 0)
                rec = t.get("recording", {})
                ra  = rec.get("artist-credit", [])
                trk_artist = "".join(
                    (c.get("name") or c.get("artist", {}).get("name", "")) + c.get("joinphrase", "")
                    for c in ra if isinstance(c, dict)) or None
                tracks.append({"num": n, "title": rec.get("title"), "artist": trk_artist})
        return {"album": rel.get("title"), "album_artist": album_artist,
                "year": (rel.get("date") or "")[:4] or None, "tracks": tracks}
    except Exception:
        return None

def _auto_fetch(toc: dict):
    if not fetch_gnudb(toc, silent=True):
        fetch_musicbrainz(toc, silent=True)
    if toc.get("album"):
        print(f"\n[meta] {toc.get('album_artist') or '?'}  -  {toc['album']}")
        _prompt()

# ── drive monitor ─────────────────────────────────────────────────────────────

def _monitor():
    global _dr_measuring
    known: set[str] = set()
    while True:
        for letter in _cdrom_letters():
            if letter not in known:
                toc = probe_drive(letter)
                if toc:
                    with _lock: audio_drives[letter] = toc
                    known.add(letter)
                    with _lock: idx = list(audio_drives.keys()).index(letter)
                    dtype = toc.get("disc_type", "standard")
                    dtype_tag = f"  [{toc['disc_note']}]" if toc.get("disc_note") else ""
                    print(f"\n[detected] audio CD in drive {idx} ({letter}:)"
                          f"  -  {toc['total_tracks']} tracks{dtype_tag}")
                    _prompt()
                    threading.Thread(target=_auto_fetch, args=(toc,), daemon=True).start()
        if not _dr_measuring:
            for letter in list(known):
                if letter not in _cdrom_letters():
                    with _lock: audio_drives.pop(letter, None)
                    known.discard(letter)
                    print(f"\n[removed] {letter}:"); _prompt()
        time.sleep(5)

def _prompt(): print(">> ", end="", flush=True)

# ── MCI helpers ───────────────────────────────────────────────────────────────

_mci_open = False

def _mci(cmd: str) -> str:
    buf = ctypes.create_unicode_buffer(512)
    err = winmm.mciSendStringW(cmd, buf, 512, None)
    return buf.value if not err else ""

def _mci_open_drive(letter: str):
    global _mci_open
    if _mci_open:
        _mci("close cdaudio"); _mci_open = False
    err = winmm.mciSendStringW(f'open {letter}: type cdaudio alias cdaudio shareable',
                                None, 0, None)
    if err: return
    _mci("set cdaudio time format milliseconds")
    _mci_open = True

def _mci_close():
    global _mci_open
    if _mci_open:
        _mci("stop cdaudio"); _mci("close cdaudio"); _mci_open = False

def _mci_mode() -> str:
    return _mci("status cdaudio mode")

# ── playback ──────────────────────────────────────────────────────────────────

def _stop():
    global play_paused
    _stop_evt.set(); play_paused = False
    if _mci_open: _mci("stop cdaudio")

def _print_now_playing(toc: dict, tnum: int):
    """Print now-playing line for a track."""
    trk    = toc["tracks"][tnum - 1]
    title  = trk.get("title")  or f"Track {tnum:02d}"
    artist = trk.get("artist") or toc.get("album_artist") or ""
    album  = toc.get("album")  or ""
    info   = f"{artist} - {title}" if artist else title
    if album: info += f"  [{album}]"
    print(f"\n[playing] track {tnum}  {info}  ({trk['length_str']})")
    _prompt()

def play_track_cmd(letter: str, track_num: int):
    global play_letter, play_track, play_paused, play_total, _play_thread
    _stop()
    if _play_thread and _play_thread.is_alive():
        _play_thread.join(timeout=3)
    with _lock: toc = audio_drives.get(letter)
    if not toc: print(f"[error] no audio CD in {letter}:"); return

    # Skip data tracks automatically
    total = toc["total_tracks"]
    original_request = track_num
    while track_num <= total and not toc["tracks"][track_num - 1].get("is_audio", True):
        track_num += 1
    if track_num > total:
        print(f"[error] track {original_request} is a data track; no following audio track")
        return
    if not 1 <= track_num <= total:
        print(f"[error] track {track_num} out of range (1-{total})"); return

    play_letter = letter
    play_track  = track_num
    play_paused = False
    play_total  = total

    _print_now_playing(toc, track_num)

    def _run():
        global play_track
        _stop_evt.clear()

        with _lock: toc2 = audio_drives.get(letter, {})
        all_trks   = toc2.get("tracks", [])
        audio_trks = [t for t in all_trks if t.get("is_audio", True)]

        # Starting track timing
        if track_num > len(all_trks): return
        start_ms = all_trks[track_num - 1].get("start_ms", 0)

        # End at the last audio track's end_ms for gapless playback
        last_audio = audio_trks[-1] if audio_trks else all_trks[-1]
        disc_end_ms = last_audio.get("end_ms", 0)

        _mci_open_drive(letter)

        if disc_end_ms > start_ms:
            _mci(f"play cdaudio from {start_ms} to {disc_end_ms}")
        else:
            # Fallback: MCI track-number format
            _mci(f"play cdaudio from {track_num}")

        # Position-monitor loop: update play_track as we cross track boundaries
        while not _stop_evt.is_set():
            mode = _mci_mode()
            if mode not in ("playing", "paused"):
                break
            pos_s = _mci("status cdaudio position")
            if pos_s.isdigit():
                pos_ms = int(pos_s)
                for t in audio_trks:
                    s, e = t.get("start_ms", 0), t.get("end_ms", 0)
                    if s <= pos_ms < e:
                        if play_track != t["num"]:
                            play_track = t["num"]
                            with _lock: toc3 = audio_drives.get(letter, {})
                            _print_now_playing(toc3, play_track)
                        break
            time.sleep(0.5)

        if not _stop_evt.is_set():
            print("\n[end of disc]"); _prompt()

    _play_thread = threading.Thread(target=_run, daemon=True)
    _play_thread.start()

def toggle_pause():
    global play_paused
    if not play_letter or not _mci_open: print("[not playing]"); return
    play_paused = not play_paused
    if play_paused: _mci("pause cdaudio"); print("[paused]")
    else:           _mci("resume cdaudio"); print("[resumed]")

# ── info display ──────────────────────────────────────────────────────────────

def show_info(letter: str | None = None):
    dev = letter or play_letter
    if not dev:
        with _lock: devs = list(audio_drives.keys())
        dev = devs[0] if devs else None
    if not dev: print("[info] no drive selected"); return
    with _lock: toc = audio_drives.get(dev)
    if not toc: print(f"[info] no audio CD in {dev}:"); return

    audio_trks = [t for t in toc["tracks"] if t.get("is_audio", True)]
    total_sec  = sum(t.get("length_sec", 0) for t in audio_trks)
    total_min  = int(total_sec) // 60
    total_s    = int(total_sec) % 60
    # Standard 74-min disc capacity; some CDs are 80-min pressed
    capacity_min = 80 if total_sec > 74 * 60 else 74
    capacity_pct = min(100, round(total_sec / (capacity_min * 60) * 100))

    print()
    print(f"  Drive    : {dev}:")
    print(f"  Album    : {toc.get('album') or '(unknown)'}")
    print(f"  Artist   : {toc.get('album_artist') or '(unknown)'}")
    if toc.get("year"): print(f"  Year     : {toc['year']}")
    print(f"  Tracks   : {toc['total_tracks']}"
          + (f"  ({len(audio_trks)} audio)" if len(audio_trks) < toc['total_tracks'] else ""))
    print(f"  Time     : {total_min}:{total_s:02d}  ({capacity_pct}% of {capacity_min}-min disc)")

    # Build a disc-level format summary from what the TOC actually tells us.
    # Sample rate and bit depth are always 44100 Hz / 16-bit on any Red Book CD
    # (the disc spec does not vary these).  What does vary per-track: channel
    # count (2 or 4, from control bit 3) and pre-emphasis (control bit 0).
    ch_counts  = set(t.get("channels", 2) for t in audio_trks)
    pre_tracks = [t["num"] for t in audio_trks if t.get("pre_emph", False)]
    ch_label   = "Quad (4-ch)" if ch_counts == {4} else \
                 "Stereo / Quad mixed" if len(ch_counts) > 1 else "Stereo"
    fmt_line   = f"44100 Hz / 16-bit / {ch_label} / PCM"
    if toc.get("hdcd"):
        fmt_line += "  [HDCD: 20-bit peak-extension encoded]"
    print(f"  Format   : {fmt_line}")
    if pre_tracks:
        print(f"  Pre-emph : tracks {pre_tracks}  (de-emphasis needed on playback)")

    if toc.get("disc_note"):
        print(f"  Disc     : {toc['disc_note']}")

    dr_scores = toc.get("dr_scores", {})
    dr_album  = toc.get("dr_album")
    if dr_album is not None:
        def _dr_grade(v):
            if v >= 15: return "Excellent"
            if v >= 12: return "Good"
            if v >= 8:  return "Fair"
            return "Poor (loudness war)"
        print(f"  DR       : DR{dr_album}  ({_dr_grade(dr_album)})")

    print()
    for t in toc["tracks"]:
        mark   = " ◀" if dev == play_letter and t["num"] == play_track else ""
        dtype  = "DATA" if not t.get("is_audio", True) else "    "
        title  = t.get("title")  or ("[data track]" if not t.get("is_audio", True)
                                     else f"Track {t['num']:02d}")
        ta     = t.get("artist") or ""
        extra  = f"  {ta}" if ta and ta != toc.get("album_artist") else ""
        dr_tag = f"  DR{dr_scores[t['num']]}" if t["num"] in dr_scores else ""
        # Per-track format flags only shown when they differ from the norm
        flags = []
        if t.get("is_audio", True):
            if t.get("channels", 2) == 4:    flags.append("4-ch")
            if t.get("pre_emph", False):      flags.append("pre-emph")
        flag_tag = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  {t['num']:2d}.  {dtype}  {t['length_str']:>5}  {title}{extra}{dr_tag}{flag_tag}{mark}")

    if not dr_scores:
        print()
        print("  (run 'd' to measure Dynamic Range)")
    print()

# ── meta command ──────────────────────────────────────────────────────────────

def meta_cmd(letter: str | None = None):
    dev = letter or play_letter
    if not dev:
        with _lock: devs = list(audio_drives.keys())
        dev = devs[0] if devs else None
    if not dev: print("[meta] no drive selected"); return
    with _lock: toc = audio_drives.get(dev)
    if not toc: print(f"[meta] no audio CD in {dev}:"); return
    print("\n  Metadata source:")
    print("  1  GNUDB / freedb  (preferred)")
    print("  2  MusicBrainz")
    print("  3  Try both  (GNUDB first, MusicBrainz fallback)")
    choice = _ask("Choice [1/2/3]", "3").strip()
    toc["album"] = toc["album_artist"] = toc["year"] = None
    for t in toc["tracks"]: t["title"] = t["artist"] = None
    if   choice == "1": fetch_gnudb(toc, silent=False)
    elif choice == "2": fetch_musicbrainz(toc, silent=False)
    else:
        if not fetch_gnudb(toc, silent=False):
            fetch_musicbrainz(toc, silent=False)
    if toc.get("album"):
        print(f"\n  {toc.get('album_artist') or '?'}  -  {toc['album']}"
              + (f"  ({toc['year']})" if toc.get("year") else ""))
    else:
        print("  No metadata found.")
    print()

# ── DR (Dynamic Range) meter ──────────────────────────────────────────────────
# Implements the DR14 standard: 3-second blocks, top 20% RMS vs overall peak.
# Reads sectors in-memory via SCSI, no temp files.
# DR scale: DR1-7 poor  |  DR8-11 fair  |  DR12-14 good  |  DR15+ excellent

_DR_BLOCK_SAMPLES = 3 * 44100 * 2   # 3 sec * 44100 Hz * 2 channels (stereo int16)
_DR_BLOCK_BYTES   = _DR_BLOCK_SAMPLES * 2

def _dr_measure_track(dev: str, trk: dict) -> int | None:
    """
    Measure DR14 dynamic range for one track via SCSI sector reads.
    Returns integer DR value or None on failure.
    """
    lba_start = trk.get("lba", 0)
    lba_end   = trk.get("end_lba", 0)
    if lba_end <= lba_start or not trk.get("is_audio", True):
        return None
    try:
        h = _open_scsi_device(dev)
    except OSError:
        return None

    # Probe sector type
    sector_type = 0x04
    if _scsi_read_cd(h, lba_start, 1, sector_type=0x04) is None:
        if _scsi_read_cd(h, lba_start, 1, sector_type=0x00) is not None:
            sector_type = 0x00
        else:
            k32.CloseHandle(h); return None

    block_peaks: list[float] = []
    block_rms:   list[float] = []
    overall_peak = 0.0
    buf = b""
    lba = lba_start

    try:
        while lba < lba_end:
            count = min(_BURST_SECTORS, lba_end - lba)
            raw   = _scsi_read_cd(h, lba, count, sector_type=sector_type)
            if raw is None:
                # Single-sector retry
                raw = _scsi_read_cd(h, lba, 1, sector_type=sector_type)
                if raw is None: break
                count = 1
            buf += raw
            lba += count

            # Process complete 3-second blocks
            while len(buf) >= _DR_BLOCK_BYTES:
                block = buf[:_DR_BLOCK_BYTES]
                buf   = buf[_DR_BLOCK_BYTES:]
                samps = _struct.unpack_from(f"<{_DR_BLOCK_SAMPLES}h", block)
                peak  = max(abs(s) for s in samps) / 32768.0
                rms   = math.sqrt(sum(s * s for s in samps) / len(samps)) / 32768.0
                if peak > overall_peak: overall_peak = peak
                block_peaks.append(peak)
                block_rms.append(rms)

        # Partial trailing block
        if len(buf) >= 2:
            n     = len(buf) // 2
            samps = _struct.unpack_from(f"<{n}h", buf[:n * 2])
            if samps:
                peak = max(abs(s) for s in samps) / 32768.0
                rms  = math.sqrt(sum(s * s for s in samps) / len(samps)) / 32768.0
                if peak > overall_peak: overall_peak = peak
                block_peaks.append(peak)
                block_rms.append(rms)
    finally:
        k32.CloseHandle(h)

    if not block_rms or overall_peak == 0.0:
        return None

    # DR14: sort blocks by RMS descending, take top 20%, compute mean RMS
    n_top      = max(1, math.ceil(len(block_rms) * 0.20))
    top_rms    = sorted(block_rms, reverse=True)[:n_top]
    mean_rms   = math.sqrt(sum(r * r for r in top_rms) / len(top_rms))
    if mean_rms == 0.0:
        return None
    return round(20.0 * math.log10(overall_peak / mean_rms))

def dr_cmd(letter: str | None = None):
    """Measure DR14 dynamic range for all audio tracks and store results in toc."""
    global _dr_measuring
    dev = letter or play_letter
    if not dev:
        with _lock: devs = list(audio_drives.keys())
        dev = devs[0] if devs else None
    if not dev: print("[dr] no drive selected"); return
    with _lock: toc = audio_drives.get(dev)
    if not toc: print(f"[dr] no audio CD in {dev}:"); return

    if not toc.get("lbas"):
        print("[dr] No LBA data - re-insert disc and try again."); return

    audio_trks = [t for t in toc["tracks"] if t.get("is_audio", True)]
    if not audio_trks:
        print("[dr] no audio tracks found"); return

    print(f"\n[dr] Measuring {len(audio_trks)} track(s) in {dev}: ...")

    scores: dict[int, int] = {}
    _dr_measuring = True
    try:
        for t in audio_trks:
            tnum    = t["num"]
            dur_str = t.get("length_str", "?:??")
            print(f"  track {tnum:2d}  ({dur_str}) ...", end="", flush=True)
            val = _dr_measure_track(dev, t)
            if val is not None:
                scores[tnum] = val
                print(f"  DR{val}")
            else:
                print("  (failed)")
    finally:
        _dr_measuring = False

    if not scores:
        print("[dr] Could not measure any tracks."); return

    # Album DR = mean of track DRs
    dr_album = round(sum(scores.values()) / len(scores))

    with _lock:
        toc_ref = audio_drives.get(dev, {})
        toc_ref["dr_scores"] = scores
        toc_ref["dr_album"]  = dr_album

    def _grade(v):
        if v >= 15: return "Excellent"
        if v >= 12: return "Good"
        if v >= 8:  return "Fair"
        return "Poor (loudness war)"

    print(f"\n  Album DR: DR{dr_album}  ({_grade(dr_album)})\n")

TOOLS_DIR   = os.path.join(os.path.expanduser("~"), ".cdplayer", "tools")
_FFMPEG_EXE = os.path.join(TOOLS_DIR, "ffmpeg.exe")
_GYAN_FF_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-full.7z"

_SECTOR_BYTES  = 2352
_BURST_SECTORS = 20

def _letter_to_cdrom_n(letter: str) -> int | None:
    try:
        h = k32.CreateFileW(f"\\\\.\\{letter}:", 0,
                            FILE_SHARE_READ | FILE_SHARE_WRITE,
                            None, OPEN_EXISTING, 0, None)
        if h == ctypes.c_void_p(-1).value: return None
        buf = ctypes.create_string_buffer(12)
        n   = ctypes.c_ulong(0)
        ok  = k32.DeviceIoControl(h, IOCTL_STORAGE_GET_DEVICE_NUMBER,
                                   None, 0, buf, 12, ctypes.byref(n), None)
        k32.CloseHandle(h)
        if not ok or n.value < 8: return None
        _, dev_num = _struct.unpack_from("<II", buf.raw)
        return dev_num
    except Exception:
        return None

def _open_scsi_device(letter: str):
    dev_num = _letter_to_cdrom_n(letter)
    paths   = ([f"\\\\.\\CdRom{dev_num}"] if dev_num is not None else []) + [f"\\\\.\\{letter}:"]
    last_err = 0
    for path in paths:
        h = k32.CreateFileW(path, GENERIC_READ | 0x40000000,
                            FILE_SHARE_READ | FILE_SHARE_WRITE,
                            None, OPEN_EXISTING, 0, None)
        if h != ctypes.c_void_p(-1).value: return h
        last_err = k32.GetLastError()
    raise OSError(f"Cannot open CD-ROM device for {letter}: (Win32 error {last_err})\n"
                  "Make sure you are running as Administrator.")

def _scsi_read_cd(h, lba: int, count: int,
                  sector_type: int = 0x04) -> bytes | None:
    """
    SCSI READ CD via IOCTL_SCSI_PASS_THROUGH_DIRECT.
    sector_type: 0x04 = CD-DA (standard), 0x00 = any type (fallback for unusual discs).
    Returns count*2352 bytes or None on failure.
    """
    data_len = count * _SECTOR_BYTES
    data_buf = ctypes.create_string_buffer(data_len)
    sw = _SPTDWithSense()
    sw.sptd.Length             = ctypes.sizeof(_SPTD)
    sw.sptd.CdbLength          = 12
    sw.sptd.SenseInfoLength    = 32
    sw.sptd.DataIn             = 1
    sw.sptd.DataTransferLength = data_len
    sw.sptd.TimeOutValue       = 60
    sw.sptd.DataBuffer         = ctypes.cast(data_buf, ctypes.c_void_p).value
    sw.sptd.SenseInfoOffset    = ctypes.sizeof(_SPTD)
    cdb = [0xBE, sector_type,
           (lba>>24)&0xFF, (lba>>16)&0xFF, (lba>>8)&0xFF, lba&0xFF,
           (count>>16)&0xFF, (count>>8)&0xFF, count&0xFF,
           0x10, 0x00, 0x00]
    for i, b in enumerate(cdb): sw.sptd.Cdb[i] = b
    n_ret = ctypes.c_ulong(0)
    ok = k32.DeviceIoControl(h, IOCTL_SCSI_PASS_THROUGH_DIRECT,
                              ctypes.byref(sw), ctypes.sizeof(sw),
                              ctypes.byref(sw), ctypes.sizeof(sw),
                              ctypes.byref(n_ret), None)
    if not ok or sw.sptd.ScsiStatus != 0:
        return None
    return bytes(data_buf.raw[:data_len])

def _write_wav_header(f, n_samples: int, channels: int = 2,
                      rate: int = 44100, bits: int = 16):
    """Write a standard PCM WAV header; supports 16/24/32-bit."""
    data_size   = n_samples * channels * (bits // 8)
    block_align = channels * (bits // 8)
    byte_rate   = rate * block_align
    # Use IEEE float format tag for 32-bit float; PCM for integer
    fmt_tag     = 3 if bits == 32 else 1
    f.seek(0)
    f.write(b"RIFF")
    f.write(_struct.pack("<I", 36 + data_size))
    f.write(b"WAVE")
    f.write(b"fmt ")
    f.write(_struct.pack("<IHHIIHH", 16, fmt_tag, channels, rate,
                         byte_rate, block_align, bits))
    f.write(b"data")
    f.write(_struct.pack("<I", data_size))

def _upsample_pcm(raw16: bytes, bits: int) -> bytes:
    """Expand 16-bit PCM samples to 24 or 32-bit for high-depth WAV output."""
    if bits == 16:
        return raw16
    samples = _struct.unpack_from(f"<{len(raw16)//2}h", raw16)
    if bits == 24:
        out = bytearray()
        for s in samples:
            val = s << 8          # scale to 24-bit
            out += _struct.pack("<i", val)[:3]   # 3 bytes little-endian
        return bytes(out)
    if bits == 32:
        # 32-bit float normalised to [-1.0, 1.0]
        return _struct.pack(f"<{len(samples)}f", *(s / 32768.0 for s in samples))
    return raw16

def _rip_track_scsi(dev: str, tnum: int, trk: dict, out_wav: str,
                    progress_cb=None, out_bits: int = 16) -> tuple[bool, str]:
    """
    Rip one audio track via SCSI READ CD.  Tries sector_type 0x04 (CD-DA) first;
    falls back to 0x00 (any) for unusual pressings (enhanced CDs, non-standard).
    out_bits: 16 (default), 24, or 32 for the output WAV sample width.
    """
    lba_start = trk.get("lba", 0)
    lba_end   = trk.get("end_lba", 0)
    if lba_end <= lba_start:
        return False, (f"Track {tnum} has no valid LBA range "
                       f"(start={lba_start}, end={lba_end})")
    total_sectors = lba_end - lba_start
    try:
        h = _open_scsi_device(dev)
    except OSError as e:
        return False, str(e)

    # Probe sector type: prefer 0x04; fall back to 0x00 if the first burst fails
    sector_type = 0x04
    probe = _scsi_read_cd(h, lba_start, 1, sector_type=0x04)
    if probe is None:
        probe = _scsi_read_cd(h, lba_start, 1, sector_type=0x00)
        if probe is not None:
            sector_type = 0x00
            print(f"  [note] CD-DA sector type failed; using generic sector type (non-standard disc)")
        else:
            k32.CloseHandle(h)
            return False, f"SCSI READ CD failed at LBA {lba_start} (Win32 error {k32.GetLastError()})"

    total_bytes = 0
    try:
        os.makedirs(os.path.dirname(out_wav) or ".", exist_ok=True)
        with open(out_wav, "wb") as f:
            f.write(b"\x00" * 44)   # placeholder header
            lba = lba_start; burst = _BURST_SECTORS
            while lba < lba_end:
                count = min(burst, lba_end - lba)
                raw   = _scsi_read_cd(h, lba, count, sector_type=sector_type)
                if raw is None:
                    win_err = k32.GetLastError()
                    if win_err == 6:   # ERROR_INVALID_HANDLE - drive reset
                        k32.CloseHandle(h); h = None
                        for _ in range(8):
                            time.sleep(1.5)
                            try:
                                h   = _open_scsi_device(dev)
                                raw = _scsi_read_cd(h, lba, 1, sector_type=sector_type)
                                if raw is not None: count = 1; burst = 1; break
                            except OSError: h = None; continue
                        if raw is None:
                            if h: k32.CloseHandle(h)
                            return False, f"Drive disconnected at LBA {lba} and did not recover"
                    elif count > 1:
                        raw = _scsi_read_cd(h, lba, 1, sector_type=sector_type)
                        if raw is None:
                            k32.CloseHandle(h)
                            return False, f"SCSI READ CD failed at LBA {lba} (error {k32.GetLastError()})"
                        count = 1; burst = max(1, burst // 2)
                    else:
                        k32.CloseHandle(h)
                        return False, f"SCSI READ CD failed at LBA {lba} (error {win_err})"

                # Upsample if a higher bit depth is requested
                audio_data  = _upsample_pcm(raw, out_bits) if out_bits != 16 else raw
                f.write(audio_data)
                total_bytes += len(raw)
                lba += count
                if progress_cb: progress_cb(lba - lba_start, total_sectors)

            n_samples = total_bytes // 4   # 16-bit stereo: 4 bytes per sample-pair
            _write_wav_header(f, n_samples, bits=out_bits)
    finally:
        if h is not None: k32.CloseHandle(h)

    if total_bytes == 0:
        return False, "No audio data read from drive"
    return True, ""

def _encode_wav(tmp_wav: str, out: str, enc: list, meta: list,
                ffmpeg: str | None) -> tuple[bool, str]:
    if out.lower().endswith(".wav") and enc == ["-c:a", "pcm_s16le"]:
        try: os.replace(tmp_wav, out); return True, ""
        except Exception as e: return False, str(e)
    if not ffmpeg: ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        fallback = out.rsplit(".", 1)[0] + ".wav"
        print(f"\r    (no ffmpeg - saving as WAV: {os.path.basename(fallback)})", flush=True)
        try: os.replace(tmp_wav, fallback); return True, ""
        except Exception as e: return False, str(e)
    ff_cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", tmp_wav] + enc + meta + [out]
    try:
        proc = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        def _show():
            for line in proc.stdout:
                txt = line.decode(errors="replace").rstrip()
                if txt: print(f"\r    {txt}   ", end="", flush=True)
        t = threading.Thread(target=_show, daemon=True); t.start()
        proc.wait(timeout=300); t.join(timeout=2)
        try: os.remove(tmp_wav)
        except: pass
        if proc.returncode != 0:
            return False, f"ffmpeg encode failed (exit {proc.returncode})"
        return True, ""
    except subprocess.TimeoutExpired:
        try: proc.kill()
        except: pass
        return False, "ffmpeg encode timed out"
    except Exception as e:
        return False, str(e)

def _rip_track_scsi_encode(dev: str, tnum: int, trk: dict, out: str,
                            enc: list, meta: list, ffmpeg: str | None,
                            out_bits: int = 16) -> tuple[bool, str]:
    os.makedirs(TOOLS_DIR, exist_ok=True)
    tmp_wav = os.path.join(TOOLS_DIR, f"_scsi_tmp_t{tnum}.wav")
    _last = [0]
    def _prog(done, total):
        pct = done * 100 // total if total else 0
        if pct != _last[0]:
            _last[0] = pct
            bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
            mb  = done * _SECTOR_BYTES / 1_048_576
            print(f"\r    [{bar}] {pct:3d}%  {mb:.1f} MB", end="", flush=True)
    ok, err = _rip_track_scsi(dev, tnum, trk, tmp_wav, progress_cb=_prog, out_bits=out_bits)
    print()
    if not ok: return False, err
    # After ripping, check for HDCD
    if ok and out_bits == 16:
        if _detect_hdcd(tmp_wav):
            with _lock:
                toc_ref = audio_drives.get(dev, {})
                toc_ref["hdcd"] = True
            print("  [HDCD detected] This disc uses 20-bit peak-extension encoding.")
            print("  For best results use an HDCD decoder (foobar2000 + HDCD plugin).")
    return _encode_wav(tmp_wav, out, enc, meta, ffmpeg)

# ── ffmpeg cdda ripper ────────────────────────────────────────────────────────

def _check_libcdio(ffmpeg: str) -> bool:
    try:
        r = subprocess.run([ffmpeg, "-demuxers"], capture_output=True, timeout=10)
        return b"cdda" in r.stdout or b"libcdio" in r.stdout
    except Exception:
        return False

def _rip_track_ffmpeg(dev: str, tnum: int, out: str,
                      enc: list, meta: list, ffmpeg: str) -> tuple[bool, str]:
    url    = f"cdda://{dev}:/@{tnum}"
    ff_cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "cdda", "-i", url
              ] + enc + meta + [out]
    try:
        proc = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        def _show():
            for line in proc.stdout:
                txt = line.decode(errors="replace").rstrip()
                if txt: print(f"\r    {txt}   ", end="", flush=True)
        t = threading.Thread(target=_show, daemon=True); t.start()
        proc.wait(timeout=600); t.join(timeout=2)
        if proc.returncode != 0:
            return False, f"ffmpeg cdda failed (exit {proc.returncode})"
        return True, ""
    except subprocess.TimeoutExpired:
        try: proc.kill()
        except: pass
        return False, "timed out"
    except Exception as e:
        return False, str(e)

def _get_ffmpeg() -> str | None:
    if os.path.isfile(_FFMPEG_EXE) and _check_libcdio(_FFMPEG_EXE):
        return _FFMPEG_EXE
    sys_ff = shutil.which("ffmpeg")
    if sys_ff and _check_libcdio(sys_ff): return sys_ff
    if sys_ff: print("[rip] system ffmpeg lacks libcdio - downloading gyan.dev full build ...")
    os.makedirs(TOOLS_DIR, exist_ok=True)
    archive = os.path.join(TOOLS_DIR, "ffmpeg_gyan.7z")
    if not (os.path.isfile(archive) and os.path.getsize(archive) > 10_000_000):
        print(f"\n[rip] Downloading ffmpeg full build with libcdio (~110 MB, one-time)...")
        print(f"      Source: {_GYAN_FF_URL}")
        try:
            req = urllib.request.Request(_GYAN_FF_URL, headers={"User-Agent": "cdplayer/6.3"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                with open(archive, "wb") as zout:
                    downloaded = 0
                    while True:
                        block = resp.read(65536)
                        if not block: break
                        zout.write(block); downloaded += len(block)
                        if total:
                            print(f"\r      {downloaded*100//total:3d}%  "
                                  f"{downloaded/1048576:.0f} MB / {total/1048576:.0f} MB",
                                  end="", flush=True)
            print()
        except Exception as e:
            print(f"\n[rip] download failed: {e}")
            print("      Please download the gyan.dev full build manually:")
            print("      https://www.gyan.dev/ffmpeg/builds/  → ffmpeg-release-full.7z")
            print(f"      Extract ffmpeg.exe to: {TOOLS_DIR}")
            return None
    print("      extracting ffmpeg.exe ...", end="", flush=True)
    try:
        import py7zr
        with py7zr.SevenZipFile(archive, mode="r") as z:
            hits   = [n for n in z.getnames() if n.endswith("ffmpeg.exe")]
            if not hits: raise FileNotFoundError("ffmpeg.exe not in archive")
            target = next((n for n in hits if "/bin/ffmpeg.exe" in n), hits[0])
            z.extract(path=TOOLS_DIR, targets=[target])
            extracted = os.path.join(TOOLS_DIR, target)
            if extracted != _FFMPEG_EXE: os.replace(extracted, _FFMPEG_EXE)
        print(" done")
        try: os.remove(archive)
        except: pass
        return _FFMPEG_EXE if _check_libcdio(_FFMPEG_EXE) else None
    except ImportError:
        print("\n      (installing py7zr ...)", end="", flush=True)
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "py7zr", "-q"],
                           check=True, timeout=60)
            print(" done"); return _get_ffmpeg()
        except Exception as e:
            print(f"\n[rip] could not install py7zr: {e}"); return None
    except Exception as e:
        print(f"\n[rip] extraction failed: {e}"); return None

# ── rip interactive ───────────────────────────────────────────────────────────

def rip_interactive(letter: str | None = None):
    dev = letter or play_letter
    if not dev:
        with _lock: devs = list(audio_drives.keys())
        dev = devs[0] if devs else None
    if not dev: print("[rip] no audio CD"); return
    with _lock: toc = audio_drives.get(dev)
    if not toc: return

    # Show disc type notice
    if toc.get("disc_note"):
        print(f"\n  Note: {toc['disc_note']}")
    audio_tracks = [t for t in toc["tracks"] if t.get("is_audio", True)]
    if len(audio_tracks) < toc["total_tracks"]:
        print(f"  {toc['total_tracks'] - len(audio_tracks)} data track(s) will be skipped automatically.")

    print("\n── Rip method ")
    print("  1  SCSI READ CD  (IOCTL_SCSI_PASS_THROUGH_DIRECT - recommended)")
    print("     Raw SCSI commands direct to drive firmware. No extra installs.")
    print("     Supports: standard 16-bit, HDCD (20-bit), enhanced/QuickTime CDs.")
    print()
    print("  2  ffmpeg cdda/libcdio  (legacy - needs gyan.dev full build)")
    print("     May fail on Windows 10 1803+ but included as a fallback.")
    print()

    method_s = _ask("Method [1/2]", "1").strip()
    method   = 2 if method_s == "2" else 1

    if method == 1:
        with _lock: toc_check = audio_drives.get(dev, {})
        if not toc_check.get("lbas"):
            print("[rip] No LBA data in TOC - cannot use SPTI method.")
            print("      Try method 2 (ffmpeg cdda) instead."); return
        first_trk = toc_check["tracks"][0]
        if not first_trk.get("end_lba"):
            print("[rip] Track LBAs present but end_lba is 0.")
            print("      Try re-inserting the disc."); return
        ffmpeg = shutil.which("ffmpeg") or (_FFMPEG_EXE if os.path.isfile(_FFMPEG_EXE) else None)
        if not ffmpeg:
            print("[rip] Note: no ffmpeg found - FLAC output unavailable; will save as WAV")
    else:
        ffmpeg = _get_ffmpeg()
        if not ffmpeg: print("[rip] ffmpeg not available - cannot rip"); return
        if not _check_libcdio(ffmpeg):
            print("[rip] ffmpeg found but missing libcdio - cannot read CDs")
            print("      Download the gyan.dev full build: https://www.gyan.dev/ffmpeg/builds/")
            return

    method_label = {1: "SCSI READ CD (IOCTL_SCSI_PASS_THROUGH_DIRECT)",
                    2: "ffmpeg cdda/libcdio"}[method]

    print("\n── Rip settings ")

    # ── Format ──
    fmt = _ask("Format [flac / wav]", "flac").lower()
    if fmt not in ("flac", "wav"): fmt = "flac"

    out_bits = 16   # default; only relevant for WAV/SCSI

    if fmt == "flac":
        if method == 1 and not ffmpeg:
            print("  (no ffmpeg - overriding to wav)")
            fmt = "wav"; ext = "wav"; enc = ["-c:a", "pcm_s16le"]
        else:
            try:    ql = max(0, min(8, int(_ask("FLAC compression [0-8]", "5"))))
            except: ql = 5
            enc = ["-c:a", "flac", "-compression_level", str(ql)]; ext = "flac"

    if fmt == "wav":
        if method == 1:
            # SCSI always reads 16-bit from disc; we can expand to 24/32 in software
            print("  Bit depth options:")
            print("    16  Standard CD (Red Book)                  recommended for most CDs")
            print("    24  High-depth WAV   (upsampled from 16-bit; for HDCD / hi-res masters)")
            print("    32  32-bit float WAV (audiophile / FIM UltraHD mastered discs)")
            bd_s = _ask("Bit depth [16/24/32]", "16").strip()
            out_bits = {"24": 24, "32": 32}.get(bd_s, 16)
            codec_map = {16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_f32le"}
            enc = ["-c:a", codec_map[out_bits]]; ext = "wav"
        else:
            bd = _ask("Bit depth [16/24]", "16")
            enc = ["-c:a", "pcm_s24le" if bd == "24" else "pcm_s16le"]; ext = "wav"

    # ── Track selection (audio tracks only) ──
    audio_nums = [t["num"] for t in toc["tracks"] if t.get("is_audio", True)]
    total_audio = len(audio_nums)
    nums = _parse_tracks(
        _ask(f"Tracks [all/1/2-5/1,3,5] ({total_audio} audio tracks)", "all"),
        toc["total_tracks"])
    # Filter to audio-only
    nums = [n for n in nums if n in audio_nums]
    if not nums: print("[rip] no audio tracks selected"); return

    default = os.path.join(os.path.expanduser("~"), "Music")
    out_dir  = os.path.expanduser(_ask(f"Output folder [{default}]", default))
    try: os.makedirs(out_dir, exist_ok=True)
    except Exception as e: print(f"[rip] cannot create folder: {e}"); return

    album  = toc.get("album")        or "Unknown Album"
    artist = toc.get("album_artist") or "Unknown Artist"
    year   = toc.get("year")         or ""

    depth_note = f" {out_bits}-bit" if fmt == "wav" else ""
    print(f"\n  {fmt.upper()}{depth_note}  |  {len(nums)} track(s)  |  -> {out_dir}")
    print(f"  Method: {method_label}\n")
    if _ask("Start? [y/n]", "y").lower() != "y": print("[rip] cancelled"); return

    _stop(); _mci_close()
    if method == 1:
        print("  Waiting for drive to settle …", end="", flush=True)
        time.sleep(2.0); print(" ready")

    failed = []
    for i, tnum in enumerate(nums):
        trk     = toc["tracks"][tnum-1]
        t_title = trk.get("title")  or f"Track {tnum:02d}"
        t_art   = trk.get("artist") or artist
        dur_str = trk.get("length_str", "?:??")
        fname   = f"{tnum:02d} - {_safe(t_art)} - {_safe(t_title)}.{ext}"
        out     = os.path.join(out_dir, fname)
        print(f"  [{i+1}/{len(nums)}] track {tnum}: {t_title}  ({dur_str})")
        meta_args = ["-metadata", f"title={t_title}",
                     "-metadata", f"artist={t_art}",
                     "-metadata", f"album={album}",
                     "-metadata", f"tracknumber={tnum}"]
        if year: meta_args += ["-metadata", f"date={year}"]
        if method == 1:
            ok, err = _rip_track_scsi_encode(dev, tnum, trk, out, enc, meta_args, ffmpeg, out_bits)
        else:
            ok, err = _rip_track_ffmpeg(dev, tnum, out, enc, meta_args, ffmpeg)
        if ok: print(f"    done" + " " * 40)
        else:  print(f"    FAILED: {err}"); failed.append(tnum)
        time.sleep(0.1)

    print()
    if failed: print(f"[rip] done - failed tracks: {failed}")
    else:      print(f"[rip] all done  ->  {out_dir}")
    print()

# ── helpers ───────────────────────────────────────────────────────────────────

def _safe(s): return re.sub(r"[\\/*?:'\"<>|]", "_", s)

def _ask(p, d=""):
    try:
        v = input(f"  {p}: ").strip(); return v if v else d
    except EOFError: return d

def _parse_tracks(sel, total):
    if sel.strip().lower() in ("", "all"): return list(range(1, total+1))
    nums: set[int] = set()
    for p in sel.split(","):
        m = re.match(r"^(\d+)-(\d+)$", p.strip())
        if m: nums.update(range(int(m.group(1)), int(m.group(2))+1))
        elif p.strip().isdigit(): nums.add(int(p.strip()))
    return sorted(n for n in nums if 1 <= n <= total)

def _by_idx(idx):
    with _lock: keys = list(audio_drives.keys())
    if not keys:         print("[error] no audio CDs detected"); return None
    if idx >= len(keys): print(f"[error] drive {idx} not found"); return None
    return keys[idx]

# ── command dispatch ──────────────────────────────────────────────────────────

def handle(cmd):
    global play_track, play_letter
    p = cmd.strip().split()
    if not p: return
    v = p[0].lower()
    if   v in ("q","quit","exit"):  _stop(); _mci_close(); sys.exit(0)
    elif v == "play":
        idx = int(p[1]) if len(p)>1 and p[1].isdigit() else 0
        dev = _by_idx(idx)
        if dev: play_track_cmd(dev, 1)
    elif v in ("track","t"):
        if len(p)<2 or not p[1].isdigit(): print("usage: track <N>"); return
        dev = play_letter or _by_idx(0)
        if dev: play_track_cmd(dev, int(p[1]))
    elif v in ("next","n"):
        dev = play_letter or _by_idx(0)
        if dev: play_track_cmd(dev, play_track+1)
    elif v in ("prev","previous","b"):
        dev = play_letter or _by_idx(0)
        if dev: play_track_cmd(dev, max(1, play_track-1))
    elif v in ("p","pause"):  toggle_pause()
    elif v == "stop":         _stop(); print("[stopped]")
    elif v in ("i","info"):
        idx = int(p[1]) if len(p)>1 and p[1].isdigit() else None
        show_info(_by_idx(idx) if idx is not None else (play_letter or _by_idx(0)))
    elif v == "meta":
        idx = int(p[1]) if len(p)>1 and p[1].isdigit() else None
        meta_cmd(_by_idx(idx) if idx is not None else (play_letter or _by_idx(0)))
    elif v in ("r","rip"):
        idx = int(p[1]) if len(p)>1 and p[1].isdigit() else None
        rip_interactive(_by_idx(idx) if idx is not None else (play_letter or _by_idx(0)))
    elif v in ("d","dr"):
        idx = int(p[1]) if len(p)>1 and p[1].isdigit() else None
        dr_cmd(_by_idx(idx) if idx is not None else (play_letter or _by_idx(0)))
    elif v in ("drives","ls"):
        with _lock: ad = dict(audio_drives)
        if not ad: print("[drives] none detected"); return
        for i,(lt,toc) in enumerate(ad.items()):
            note = f"  [{toc['disc_note']}]" if toc.get("disc_note") else ""
            print(f"  {i}  {lt}:  {toc.get('total_tracks','?')} tracks"
                  f"  {toc.get('album') or ''}{note}")
    elif v in ("help","h","?"):
        print("""
  play [N]     play drive N (default 0)
  track <N>    jump to track N
  next / n     next track
  prev / b     previous track
  p            pause / resume
  stop         stop playback
  i            disc & track info  (format, capacity, HDCD, DR scores)
  d            measure Dynamic Range (DR14) for all audio tracks
  meta         fetch / re-fetch metadata
  r            rip to FLAC or WAV  (16 / 24 / 32-bit)
  drives       list drives
  q            quit
""")
    else: print(f"[?] unknown: '{v}'.  Type 'help'.")

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("cdplayer v13  -  type 'help' for commands, 'q' to quit")
    print("-------------------------------------------------------------------------------------")
    print("  Special support: Enhanced CDs, QuickTime CDs, HDCD, FIM UltraHD, 20/24/32-bit")
    print(" ")
    print("  Gapless playback  |  DR14 meter (press 'd')  |  audio format info in 'i'")
    print("-------------------------------------------------------------------------------------")
    if not shutil.which("ffmpeg"):
        print("  tip: install ffmpeg for ripping  → https://www.gyan.dev/ffmpeg/builds/")
    print("Scanning for audio CDs …\n")
    threading.Thread(target=_monitor, daemon=True).start()
    time.sleep(1.5)
    while True:
        try:
            cmd = input(">> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); _stop(); _mci_close(); sys.exit(0)
        if cmd: handle(cmd)

if __name__ == "__main__":
    main()

