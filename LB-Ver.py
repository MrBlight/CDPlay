#!/usr/bin/env python3
"""
CDPlayer Multi-Platform v1.16.1 - Linux Version (LB-Ver.py)
Engine: Linux ioctl + cdparanoia/ffmpeg for ripping
"""
import sys
import os
import json
import math
import base64
import hashlib
import struct
import time
import threading
import subprocess
import shutil
import fcntl
import ctypes
import tempfile
import wave
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

DEBUG_BYPASS = False
if '--linux-debug' in sys.argv:
    DEBUG_BYPASS = True
    sys.argv.remove('--linux-debug')
if not DEBUG_BYPASS and sys.platform != 'linux':
    print(f"\n[ERROR] You are running the Linux version on '{sys.platform}'.")
    print("This version (LB-Ver.py) is designed for Linux-based systems only.\n")
    print("Please use:\n  • Win-Ver.py    for Microsoft Windows\n  • Mac-Ver.py    for macOS\n  • BSD-Ver.py    for FreeBSD/OpenBSD/NetBSD\n")
    print("Run with --linux-debug to bypass this check (for development).")
    sys.exit(1)

CDROMPAUSE = 0x5301
CDROMRESUME = 0x5302
CDROMPLAYMSF = 0x5303
CDROMPLAYTRKIND = 0x5304
CDROMREADTOCHDR = 0x5305
CDROMREADTOCENTRY = 0x5306
CDROMSTOP = 0x5307
CDROMSTART = 0x5308
CDROMEJECT = 0x5309
CDROMVOLCTRL = 0x530A
CDROMSUBCHNL = 0x530B
CDROMREADMODE2 = 0x530C
CDROMREADMODE1 = 0x530D
CDROMREADAUDIO = 0x530E
CDROMMULTISESSION = 0x5310
CDROM_GET_MCN = 0x5311
CDROMRESET = 0x5312
CDROMVOLREAD = 0x5313
CDROMREADRAW = 0x5314
CDROMSEEK = 0x5316
CDROMCLOSETRAY = 0x5319
CDROM_SET_OPTIONS = 0x5320
CDROM_CLEAR_OPTIONS = 0x5321
CDROM_SELECT_SPEED = 0x5322
CDROM_SELECT_DISC = 0x5323
CDROM_MEDIA_CHANGE = 0x5325
CDROM_DRIVE_STATUS = 0x5326
CDROM_DISC_STATUS = 0x5327
CDROM_LOCKDOOR = 0x5329

CDROM_LEADOUT = 0xAA
CDROM_DATA_TRACK = 0x04
CDS_NO_INFO = 0
CDS_NO_DISC = 1
CDS_TRAY_OPEN = 2
CDS_DRIVE_NOT_READY = 3
CDS_DISC_OK = 4
CDROM_LBA = 0x01
CDROM_MSF = 0x02
CDROM_FRAMESIZE_RAW = 2352
CDROM_FRAMES = 75
CDROM_MSF_OFFSET = 150
CDROM_AUDIO_PLAY = 0x11
CDROM_AUDIO_PAUSED = 0x12
CDROM_AUDIO_COMPLETED = 0x13

class cdrom_tochdr(ctypes.Structure):
    _fields_ = [('cdth_trk0', ctypes.c_ubyte), ('cdth_trk1', ctypes.c_ubyte)]

class cdrom_tocentry(ctypes.Structure):
    _fields_ = [
        ('cdte_track', ctypes.c_ubyte),
        ('cdte_adr', ctypes.c_ubyte, 4),
        ('cdte_ctrl', ctypes.c_ubyte, 4),
        ('cdte_format', ctypes.c_ubyte),
        ('cdte_addr', ctypes.c_int),
        ('cdte_datamode', ctypes.c_ubyte),
    ]

class cdrom_msf(ctypes.Structure):
    _fields_ = [
        ('cdmsf_min0', ctypes.c_ubyte), ('cdmsf_sec0', ctypes.c_ubyte), ('cdmsf_frame0', ctypes.c_ubyte),
        ('cdmsf_min1', ctypes.c_ubyte), ('cdmsf_sec1', ctypes.c_ubyte), ('cdmsf_frame1', ctypes.c_ubyte),
    ]

class cdrom_ti(ctypes.Structure):
    _fields_ = [('cdti_trk0', ctypes.c_ubyte), ('cdti_ind0', ctypes.c_ubyte), ('cdti_trk1', ctypes.c_ubyte), ('cdti_ind1', ctypes.c_ubyte)]

class cdrom_subchnl(ctypes.Structure):
    class Addr(ctypes.Union):
        _fields_ = [('msf', ctypes.c_ubyte * 3), ('lba', ctypes.c_int)]
    _fields_ = [
        ('cdsc_format', ctypes.c_ubyte), ('cdsc_audiostatus', ctypes.c_ubyte),
        ('cdsc_adr', ctypes.c_ubyte, 4), ('cdsc_ctrl', ctypes.c_ubyte, 4),
        ('cdsc_trk', ctypes.c_ubyte), ('cdsc_ind', ctypes.c_ubyte),
        ('cdsc_absaddr', Addr), ('cdsc_reladdr', Addr),
    ]

class cdrom_volctrl(ctypes.Structure):
    _fields_ = [('channel0', ctypes.c_ubyte), ('channel1', ctypes.c_ubyte), ('channel2', ctypes.c_ubyte), ('channel3', ctypes.c_ubyte)]

class cdrom_read_audio(ctypes.Structure):
    class Addr(ctypes.Union):
        _fields_ = [('msf', ctypes.c_ubyte * 3), ('lba', ctypes.c_int)]
    _fields_ = [('addr', Addr), ('addr_format', ctypes.c_ubyte), ('nframes', ctypes.c_int), ('buf', ctypes.POINTER(ctypes.c_ubyte))]

_lock = threading.RLock()
audio_drives = {}
play_device = None
play_track = 1
play_paused = False
play_total = 0
_play_started = 0.0
_play_end_lba = 0
_stop_evt = threading.Event()
_play_thread = None
_cdrom_fd = None
_monitor_stop = threading.Event()


def msf_to_frames(m, s, f):
    return (m * 60 + s) * 75 + f


def frames_to_msf(frames):
    frames = max(0, int(frames))
    return frames // 4500, (frames % 4500) // 75, frames % 75


def fmt_time(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 60}:{seconds % 60:02d}'


def find_cdrom_devices():
    devices = []
    for name in ['sr0', 'sr1', 'sr2', 'sr3', 'cdrom', 'cdrw', 'dvd', 'dvdrw']:
        path = f'/dev/{name}'
        if os.path.exists(path) and path not in devices:
            devices.append(path)
    for i in range(1, 16):
        path = f'/dev/cdrom{i}'
        if os.path.exists(path) and path not in devices:
            devices.append(path)
    return devices


def open_cdrom(device):
    try:
        return os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        try:
            return os.open(device, os.O_RDONLY)
        except OSError:
            return None


def close_cdrom(fd):
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def get_drive_status(fd):
    try:
        return int(fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0))
    except OSError:
        return CDS_NO_INFO


def read_toc_header(fd):
    hdr = cdrom_tochdr()
    try:
        fcntl.ioctl(fd, CDROMREADTOCHDR, hdr)
        return hdr.cdth_trk0, hdr.cdth_trk1
    except OSError:
        return None


def read_toc_entry(fd, track):
    entry = cdrom_tocentry()
    entry.cdte_track = track
    entry.cdte_format = CDROM_LBA
    try:
        fcntl.ioctl(fd, CDROMREADTOCENTRY, entry)
        return {'track': track, 'lba': int(entry.cdte_addr), 'is_audio': not bool(entry.cdte_ctrl & CDROM_DATA_TRACK), 'ctrl': int(entry.cdte_ctrl)}
    except OSError:
        return None


def read_mcn(fd):
    class Mcn(ctypes.Structure):
        _fields_ = [('medium_catalog_number', ctypes.c_ubyte * 14)]
    mcn = Mcn()
    try:
        fcntl.ioctl(fd, CDROM_GET_MCN, mcn)
        raw = bytes(mcn.medium_catalog_number)
        return raw.split(b'\0', 1)[0].decode('ascii', 'replace').strip() or None
    except OSError:
        return None


def compute_disc_id(tracks, leadout_lba):
    audio = [t for t in tracks if t['is_audio']]
    if not audio:
        return None
    first = min(t['num'] for t in audio)
    last = max(t['num'] for t in audio)
    offsets = {t['num']: int(t['lba']) + CDROM_MSF_OFFSET for t in audio}
    payload = f'{first:02X}{last:02X}'
    payload += f'{int(leadout_lba) + CDROM_MSF_OFFSET:08X}'
    for n in range(1, 100):
        payload += f'{offsets.get(n, 0):08X}'
    digest = hashlib.sha1(payload.encode('ascii')).digest()
    return base64.b64encode(digest).decode('ascii').replace('+', '.').replace('/', '_').replace('=', '-').rstrip('=')


def probe_drive(device):
    fd = open_cdrom(device)
    if fd is None:
        return None
    try:
        if get_drive_status(fd) != CDS_DISC_OK:
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
        leadout_entry = read_toc_entry(fd, CDROM_LEADOUT)
        leadout_lba = leadout_entry['lba'] if leadout_entry else 0
        if not tracks:
            return None
        track_info = []
        for i, t in enumerate(tracks):
            end_lba = tracks[i + 1]['lba'] if i + 1 < len(tracks) else leadout_lba
            length_sec = max(0, end_lba - t['lba']) / 75.0
            track_info.append({
                'num': t['track'], 'lba': t['lba'], 'end_lba': end_lba,
                'is_audio': t['is_audio'], 'length_sec': length_sec,
                'length_str': fmt_time(length_sec), 'title': None, 'artist': None,
            })
        mcn = read_mcn(fd)
        disc_id = compute_disc_id(track_info, leadout_lba)
        return {
            'device': device, 'total_tracks': len(track_info), 'tracks': track_info,
            'album': None, 'album_artist': None, 'year': None, 'genre': None,
            'hdcd': False, 'dr_scores': {}, 'dr_album': None, 'disc_id': disc_id,
            'mcn': mcn, 'leadout_lba': leadout_lba,
        }
    finally:
        close_cdrom(fd)


def read_subchannel(fd):
    sub = cdrom_subchnl()
    sub.cdsc_format = CDROM_LBA
    try:
        fcntl.ioctl(fd, CDROMSUBCHNL, sub)
        return {
            'status': int(sub.cdsc_audiostatus), 'track': int(sub.cdsc_trk), 'index': int(sub.cdsc_ind),
            'abs_lba': int(sub.cdsc_absaddr.lba), 'rel_lba': int(sub.cdsc_reladdr.lba),
        }
    except OSError:
        return None


def _play_ioctl(fd, start_lba, end_lba):
    s = frames_to_msf(start_lba + CDROM_MSF_OFFSET)
    e = frames_to_msf(end_lba + CDROM_MSF_OFFSET)
    msf = cdrom_msf(s[0], s[1], s[2], e[0], e[1], e[2])
    fcntl.ioctl(fd, CDROMPLAYMSF, msf)


def _play_monitor(fd, device, start_track):
    global play_track
    while not _stop_evt.wait(0.35):
        sub = read_subchannel(fd)
        if sub:
            with _lock:
                play_track = sub['track'] or play_track
        else:
            continue
        if sub['status'] in (CDROM_AUDIO_COMPLETED,):
            break
    if not _stop_evt.is_set():
        print('\n[end of disc]')


def play_track_cmd(device, track_num):
    global play_device, play_track, play_paused, play_total, _play_thread, _cdrom_fd, _play_started, _play_end_lba
    stop_playback(silent=True)
    with _lock:
        toc = audio_drives.get(device)
    if not toc:
        print(f'[error] no audio CD in {device}')
        return
    total = toc['total_tracks']
    original_request = track_num
    while track_num <= total and not toc['tracks'][track_num - 1].get('is_audio', True):
        track_num += 1
    if track_num > total:
        print(f'[error] track {original_request} is a data track; no following audio track')
        return
    if not 1 <= track_num <= total:
        print(f'[error] track {track_num} out of range (1-{total})')
        return
    fd = open_cdrom(device)
    if fd is None:
        print(f'[error] cannot open {device}')
        return
    _cdrom_fd = fd
    play_device = device
    play_track = track_num
    play_paused = False
    play_total = total
    trk = toc['tracks'][track_num - 1]
    _play_started = time.monotonic()
    _play_end_lba = trk['end_lba']
    print(f"\n[playing] track {track_num}  {trk.get('title') or f'Track {track_num:02d}'}  ({trk['length_str']})")

    def runner():
        try:
            _stop_evt.clear()
            _play_ioctl(fd, trk['lba'], trk['end_lba'])
            _play_monitor(fd, device, track_num)
        except OSError as e:
            if not _stop_evt.is_set():
                print(f'[error] playback error: {e}')
        finally:
            if _stop_evt.is_set():
                return
            close_cdrom(fd)
            with _lock:
                if _cdrom_fd == fd:
                    globals()['_cdrom_fd'] = None

    _play_thread = threading.Thread(target=runner, daemon=True)
    _play_thread.start()


def toggle_pause():
    global play_paused
    if _cdrom_fd is None:
        print('[not playing]')
        return
    try:
        if play_paused:
            fcntl.ioctl(_cdrom_fd, CDROMRESUME)
            play_paused = False
            print('[resumed]')
        else:
            fcntl.ioctl(_cdrom_fd, CDROMPAUSE)
            play_paused = True
            print('[paused]')
    except OSError as e:
        print(f'[error] {e}')


def stop_playback(silent=False):
    global play_paused, _cdrom_fd, _play_thread
    _stop_evt.set()
    play_paused = False
    fd = _cdrom_fd
    _cdrom_fd = None
    if fd is not None:
        try:
            fcntl.ioctl(fd, CDROMSTOP)
        except OSError:
            pass
        close_cdrom(fd)
    if _play_thread and _play_thread.is_alive() and _play_thread is not threading.current_thread():
        _play_thread.join(timeout=1.5)
    _play_thread = None
    if not silent:
        print('[stopped]')


def eject_device(device=None):
    dev = device or play_device
    if not dev:
        devices = find_cdrom_devices()
        dev = devices[0] if devices else None
    if not dev:
        print('[eject] no drive found')
        return
    stop_playback(silent=True)
    fd = open_cdrom(dev)
    if fd is None:
        print(f'[eject] cannot open {dev}')
        return
    try:
        fcntl.ioctl(fd, CDROMEJECT)
        print(f'[eject] {dev}')
    except OSError as e:
        print(f'[eject] failed: {e}')
    finally:
        close_cdrom(fd)


def lock_door(device=None, locked=True):
    dev = device or play_device
    if not dev:
        print('[lock] no drive selected')
        return
    fd = open_cdrom(dev)
    if fd is None:
        print(f'[lock] cannot open {dev}')
        return
    try:
        fcntl.ioctl(fd, CDROM_LOCKDOOR, 1 if locked else 0)
        print(f"[lock] {'locked' if locked else 'unlocked'} {dev}")
    except OSError as e:
        print(f'[lock] failed: {e}')
    finally:
        close_cdrom(fd)


def _musicbrainz_get(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'CDPlayer/1.16.1 (Linux CD audio utility)',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_metadata(device=None):
    dev = device or play_device
    if not dev:
        with _lock:
            devs = list(audio_drives)
        dev = devs[0] if devs else None
    if not dev:
        print('[meta] no drive selected')
        return
    with _lock:
        toc = audio_drives.get(dev)
    if not toc:
        print(f'[meta] no audio CD in {dev}')
        return
    disc_id = toc.get('disc_id')
    if not disc_id:
        print('[meta] could not calculate disc ID')
        return
    print('[meta] Fetching MusicBrainz metadata...')
    try:
        params = urllib.parse.urlencode({'inc': 'artists+recordings+release-groups', 'fmt': 'json'})
        data = _musicbrainz_get(f'https://musicbrainz.org/ws/2/discid/{urllib.parse.quote(disc_id, safe="")}?{params}')
        releases = data.get('releases', [])
        if not releases:
            print('[meta] no matching MusicBrainz release')
            return
        release = releases[0]
        media = release.get('media', [])
        medium = media[0] if media else {}
        recordings = medium.get('tracks', [])
        album = release.get('title')
        artist_credit = release.get('artist-credit', [])
        album_artist = ''.join(x.get('name') or x.get('artist', {}).get('name', '') for x in artist_credit).strip() or None
        date = release.get('date') or release.get('release-events', [{}])[0].get('date')
        year = int(date[:4]) if date and date[:4].isdigit() else None
        with _lock:
            toc['album'] = album
            toc['album_artist'] = album_artist
            toc['year'] = year
            for idx, item in enumerate(recordings):
                if idx >= len(toc['tracks']):
                    break
                rec = item.get('recording', item)
                title = rec.get('title') or item.get('title')
                ac = rec.get('artist-credit', [])
                artist = ''.join(x.get('name') or x.get('artist', {}).get('name', '') for x in ac).strip() or album_artist
                if title:
                    toc['tracks'][idx]['title'] = title
                toc['tracks'][idx]['artist'] = artist
            toc['metadata_source'] = 'MusicBrainz'
        print(f'[meta] {album_artist or "Unknown Artist"} - {album or "Unknown Album"}')
    except Exception as e:
        print(f'[meta] failed: {e}')


def _read_audio_frames(device, start_lba, frame_count):
    fd = open_cdrom(device)
    if fd is None:
        raise OSError(f'cannot open {device}')
    try:
        remaining = frame_count
        cur = start_lba
        while remaining:
            n = min(75, remaining)
            buf = (ctypes.c_ubyte * (n * CDROM_FRAMESIZE_RAW))()
            ra = cdrom_read_audio()
            ra.addr_format = CDROM_LBA
            ra.addr.lba = cur
            ra.nframes = n
            ra.buf = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
            fcntl.ioctl(fd, CDROMREADAUDIO, ra)
            yield bytes(buf)
            cur += n
            remaining -= n
    finally:
        close_cdrom(fd)


def _sample_stats(pcm):
    if not pcm:
        return 0, 0.0
    count = len(pcm) // 2
    vals = struct.unpack('<%dh' % count, pcm)
    peak = max(abs(v) for v in vals)
    mean_sq = sum(v * v for v in vals) / count
    return peak, math.sqrt(mean_sq)


def _dr_from_pcm_chunks(chunks):
    window_bytes = 3 * 44100 * 2 * 2
    buf = bytearray()
    windows = []
    peak = 0
    for chunk in chunks:
        buf.extend(chunk)
        while len(buf) >= window_bytes:
            window = bytes(buf[:window_bytes])
            del buf[:window_bytes]
            p, rms = _sample_stats(window)
            peak = max(peak, p)
            if rms > 0:
                windows.append(20 * math.log10(rms / 32768.0))
    if buf:
        p, rms = _sample_stats(bytes(buf))
        peak = max(peak, p)
        if rms > 0:
            windows.append(20 * math.log10(rms / 32768.0))
    if not windows or peak <= 0:
        return None
    windows.sort(reverse=True)
    take = max(1, math.ceil(len(windows) * 0.20))
    loud_rms = sum(windows[:take]) / take
    peak_db = 20 * math.log10(peak / 32767.0)
    return max(0, int(round(peak_db - loud_rms)))


def measure_track_dr(device, track):
    frames = max(0, int(track['end_lba'] - track['lba']))
    return _dr_from_pcm_chunks(_read_audio_frames(device, track['lba'], frames))


def measure_dr(device=None, track_num=None):
    dev = device or play_device
    if not dev:
        with _lock:
            devs = list(audio_drives)
        dev = devs[0] if devs else None
    if not dev:
        print('[dr] no audio CD')
        return
    with _lock:
        toc = audio_drives.get(dev)
    if not toc:
        print(f'[dr] no audio CD in {dev}')
        return
    tracks = [t for t in toc['tracks'] if t.get('is_audio', True)]
    if track_num is not None:
        tracks = [t for t in tracks if t['num'] == track_num]
    if not tracks:
        print('[dr] no matching audio tracks')
        return
    print('[dr] Measuring Dynamic Range (DR14-style)...')
    scores = {}
    for i, trk in enumerate(tracks, 1):
        try:
            score = measure_track_dr(dev, trk)
            scores[trk['num']] = score
            trk['dr'] = score
            print(f"  track {trk['num']:02d}: DR{score if score is not None else '--'}")
        except Exception as e:
            print(f"  track {trk['num']:02d}: FAILED: {e}")
    with _lock:
        toc['dr_scores'].update(scores)
        good = [x for x in scores.values() if x is not None]
        toc['dr_album'] = round(sum(good) / len(good), 1) if good else None
    if toc.get('dr_album') is not None:
        print(f"[dr] album average: DR{toc['dr_album']:.1f}")


def detect_hdcd(device, track=None):
    if not shutil.which('ffmpeg'):
        print('[hdcd] ffmpeg not found')
        return None
    with _lock:
        toc = audio_drives.get(device)
    if not toc:
        return None
    if track is None:
        tracks = [t for t in toc['tracks'] if t.get('is_audio', True)]
    else:
        tracks = [t for t in toc['tracks'] if t['num'] == track and t.get('is_audio', True)]
    found = False
    for trk in tracks:
        cmd = [
            'ffmpeg', '-hide_banner', '-nostats', '-loglevel', 'info',
            '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo', '-t', '0.01',
            '-f', 'null', '-',
        ]
        del cmd
        try:
            chunks = _read_audio_frames(device, trk['lba'], min(150, int(trk['end_lba'] - trk['lba'])))
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
                tmp = f.name
            try:
                write_wav_from_pcm(tmp, chunks)
                p = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'info', '-i', tmp, '-af', 'hdcd=disable_autoconvert=1', '-f', 'null', '-'], capture_output=True, text=True, timeout=30)
                text = (p.stdout or '') + '\n' + (p.stderr or '')
                yes = 'HDCD detected: yes' in text or 'HDCD detected: detected' in text
                trk['hdcd'] = yes
                found = found or yes
                print(f"  track {trk['num']:02d}: {'HDCD' if yes else 'not detected'}")
            finally:
                try: os.unlink(tmp)
                except OSError: pass
        except Exception as e:
            print(f"  track {trk['num']:02d}: FAILED: {e}")
    with _lock:
        toc['hdcd'] = found
    return found


def write_wav_from_pcm(path, chunks, sample_rate=44100, channels=2):
    with wave.open(path, 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        for chunk in chunks:
            w.writeframes(chunk)


def _run_rip_command(device, track_num, out_path, method, fmt, title, album, artist):
    if method == '1':
        if fmt == 'wav':
            return subprocess.run(['cdparanoia', '-q', str(track_num), out_path], capture_output=True, text=True, timeout=900)
        with tempfile.TemporaryDirectory(prefix='cdplayer-rip-') as td:
            wav_path = os.path.join(td, f'{track_num:02d}.wav')
            result = subprocess.run(['cdparanoia', '-q', str(track_num), wav_path], capture_output=True, text=True, timeout=900)
            if result.returncode != 0:
                return result
            return subprocess.run([
                'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', wav_path,
                '-c:a', 'flac', '-metadata', f'title={title}', '-metadata', f'artist={artist}',
                '-metadata', f'album={album}', '-metadata', f'track={track_num}', out_path
            ], capture_output=True, text=True, timeout=900)
    codec = 'pcm_s16le' if fmt == 'wav' else 'flac'
    return subprocess.run([
        'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'cdda', '-i', f'cdda:{device}',
        '-map', '0:a:0', '-c:a', codec, '-metadata', f'title={title}', '-metadata', f'artist={artist}',
        '-metadata', f'album={album}', '-metadata', f'track={track_num}', out_path
    ], capture_output=True, text=True, timeout=900)


def rip_interactive(device=None):
    dev = device or play_device
    if not dev:
        with _lock:
            devs = list(audio_drives)
        dev = devs[0] if devs else None
    if not dev:
        print('[rip] no audio CD')
        return
    with _lock:
        toc = audio_drives.get(dev)
    if not toc:
        print(f'[rip] no audio CD in {dev}')
        return
    if shutil.which('cdparanoia'):
        default_method = '1'
    elif shutil.which('ffmpeg'):
        default_method = '2'
    else:
        print('[rip] neither cdparanoia nor ffmpeg found')
        return
    print('\n── Rip method ')
    print('  1  cdparanoia  (recommended)')
    print('  2  ffmpeg cdda')
    print()
    method = input(f'Method [1/2] [{default_method}]: ').strip() or default_method
    if method == '1' and not shutil.which('cdparanoia'):
        print('[rip] cdparanoia not found')
        return
    if method != '1' and not shutil.which('ffmpeg'):
        print('[rip] ffmpeg not found')
        return
    fmt = (input('  Format [flac/wav]: ').strip().lower() or 'flac')
    if fmt not in ('flac', 'wav'):
        fmt = 'flac'
    audio_nums = [t['num'] for t in toc['tracks'] if t.get('is_audio', True)]
    track_sel = input(f'  Tracks [all/1/2-5/1,3,5] ({len(audio_nums)} audio tracks): ').strip() or 'all'
    if track_sel.lower() == 'all':
        nums = audio_nums
    else:
        nums = []
        for part in track_sel.split(','):
            part = part.strip()
            try:
                if '-' in part:
                    a, b = map(int, part.split('-', 1))
                    nums.extend(range(a, b + 1))
                else:
                    nums.append(int(part))
            except ValueError:
                pass
        nums = [n for n in nums if n in audio_nums]
    if not nums:
        print('[rip] no audio tracks selected')
        return
    out_dir = os.path.expanduser(input('  Output folder [~/Music]: ').strip() or '~/Music')
    os.makedirs(out_dir, exist_ok=True)
    album = toc.get('album') or 'Unknown Album'
    artist = toc.get('album_artist') or 'Unknown Artist'
    print(f'\n  {fmt.upper()}  |  {len(nums)} track(s)  |  -> {out_dir}\n')
    if input('  Start? [y/n]: ').strip().lower() != 'y':
        print('[rip] cancelled')
        return
    stop_playback(silent=True)
    failed = []
    for i, tnum in enumerate(nums):
        trk = toc['tracks'][tnum - 1]
        title = trk.get('title') or f'Track {tnum:02d}'
        safe_title = ''.join(c if c.isalnum() or c in ' -_' else '_' for c in title).strip() or f'Track {tnum:02d}'
        out_path = os.path.join(out_dir, f'{tnum:02d} - {safe_title}.{fmt}')
        print(f'  [{i + 1}/{len(nums)}] track {tnum}: {title}  ({trk["length_str"]})')
        try:
            result = _run_rip_command(dev, tnum, out_path, method, fmt, title, album, artist)
            if result.returncode == 0:
                print('    done')
            else:
                msg = (result.stderr or result.stdout or 'unknown error').strip().splitlines()[-1:]
                print(f'    FAILED: {msg[0] if msg else "unknown error"}')
                failed.append(tnum)
        except Exception as e:
            print(f'    FAILED: {e}')
            failed.append(tnum)
    print()
    print(f"[rip] {'done' if not failed else 'done with failures'}  ->  {out_dir}")
    if failed:
        print(f'[rip] failed tracks: {failed}')


def current_position():
    if _cdrom_fd is None:
        return None
    sub = read_subchannel(_cdrom_fd)
    if sub:
        return sub
    return None


def show_info(device=None):
    dev = device or play_device
    if not dev:
        with _lock:
            devs = list(audio_drives)
        dev = devs[0] if devs else None
    if not dev:
        print('[info] no drive selected')
        return
    with _lock:
        toc = audio_drives.get(dev)
    if not toc:
        print(f'[info] no audio CD in {dev}')
        return
    audio_tracks = [t for t in toc['tracks'] if t.get('is_audio', True)]
    total_sec = sum(t.get('length_sec', 0) for t in audio_tracks)
    print()
    print(f"  Device   : {dev}")
    print(f"  Album    : {toc.get('album') or '(unknown)'}")
    print(f"  Artist   : {toc.get('album_artist') or '(unknown)'}")
    if toc.get('year'):
        print(f"  Year     : {toc['year']}")
    print(f"  Disc ID  : {toc.get('disc_id') or '(unknown)'}")
    if toc.get('mcn'):
        print(f"  MCN      : {toc['mcn']}")
    print(f"  Tracks   : {toc['total_tracks']}")
    print(f"  Time     : {fmt_time(total_sec)}")
    print(f"  Format   : 44100 Hz / 16-bit / Stereo / PCM")
    print(f"  HDCD     : {'yes' if toc.get('hdcd') else 'not detected'}")
    if toc.get('dr_album') is not None:
        print(f"  Album DR : DR{toc['dr_album']:.1f}")
    print()
    pos = current_position()
    if pos:
        print(f"  Position : track {pos['track']:02d}  {fmt_time(pos['rel_lba'] / 75)}")
        print()
    for t in toc['tracks']:
        mark = ' ◀' if dev == play_device and t['num'] == play_track else ''
        dtype = 'DATA' if not t.get('is_audio', True) else '    '
        title = t.get('title') or f"Track {t['num']:02d}"
        dr = f" DR{t['dr']}" if t.get('dr') is not None else ''
        hd = ' HDCD' if t.get('hdcd') else ''
        print(f"  {t['num']:2d}.  {dtype}  {t['length_str']:>5}  {title}{dr}{hd}{mark}")
    print()


def save_state(path):
    with _lock:
        data = {'version': '1.16.1', 'audio_drives': audio_drives}
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def load_state(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    drives = data.get('audio_drives', {})
    with _lock:
        audio_drives.clear()
        audio_drives.update(drives)
    print(f"[load] loaded {len(drives)} drive state(s)")


def _metadata_background(device):
    try:
        fetch_metadata(device)
    except Exception as e:
        print(f'[meta] background fetch failed: {e}')


def _monitor():
    known = set()
    while not _monitor_stop.is_set():
        devices = find_cdrom_devices()
        for device in devices:
            if device not in known:
                toc = probe_drive(device)
                if toc:
                    with _lock:
                        audio_drives[device] = toc
                    print(f"\n[detected] audio CD in {device}  -  {toc['total_tracks']} tracks")
                    threading.Thread(target=_metadata_background, args=(device,), daemon=True).start()
                known.add(device)
            else:
                with _lock:
                    known_toc = audio_drives.get(device)
                if known_toc is None:
                    toc = probe_drive(device)
                    if toc:
                        with _lock:
                            audio_drives[device] = toc
                        threading.Thread(target=_metadata_background, args=(device,), daemon=True).start()
        for device in list(known):
            if device not in devices or not os.path.exists(device):
                with _lock:
                    audio_drives.pop(device, None)
                known.discard(device)
                print(f'\n[removed] {device}')
        _monitor_stop.wait(3)


def handle(cmd):
    global play_track, play_device
    parts = cmd.strip().split()
    if not parts:
        return
    v = parts[0].lower()
    try:
        if v in ('q', 'quit', 'exit'):
            _monitor_stop.set()
            stop_playback()
            sys.exit(0)
        if v == 'play':
            idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            with _lock:
                keys = list(audio_drives)
            if idx < len(keys):
                play_track_cmd(keys[idx], 1)
            else:
                print(f'[error] drive {idx} not found')
        elif v in ('track', 't'):
            if len(parts) < 2 or not parts[1].isdigit():
                print('usage: track <N>')
                return
            dev = play_device
            if not dev:
                with _lock: dev = next(iter(audio_drives), None)
            if dev: play_track_cmd(dev, int(parts[1]))
        elif v in ('next', 'n'):
            dev = play_device
            if not dev:
                with _lock: dev = next(iter(audio_drives), None)
            if dev: play_track_cmd(dev, play_track + 1)
        elif v in ('prev', 'previous', 'b'):
            dev = play_device
            if not dev:
                with _lock: dev = next(iter(audio_drives), None)
            if dev: play_track_cmd(dev, max(1, play_track - 1))
        elif v in ('p', 'pause'):
            toggle_pause()
        elif v == 'stop':
            stop_playback()
        elif v in ('i', 'info'):
            show_info(parts[1] if len(parts) > 1 else None)
        elif v == 'meta':
            fetch_metadata(parts[1] if len(parts) > 1 else None)
        elif v in ('r', 'rip'):
            rip_interactive(parts[1] if len(parts) > 1 else None)
        elif v in ('d', 'dr'):
            measure_dr(track_num=int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None)
        elif v in ('hdcd', 'hd'):
            track = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            dev = play_device
            if not dev:
                with _lock: dev = next(iter(audio_drives), None)
            if dev: detect_hdcd(dev, track)
        elif v in ('drives', 'ls'):
            with _lock: ad = dict(audio_drives)
            if not ad:
                print('[drives] none detected')
            else:
                for i, (dev, toc) in enumerate(ad.items()):
                    print(f"  {i}  {dev}  {toc['total_tracks']} tracks  {toc.get('album') or ''}")
        elif v == 'eject':
            eject_device(parts[1] if len(parts) > 1 else None)
        elif v == 'lock':
            lock_door(parts[1] if len(parts) > 1 else None, True)
        elif v == 'unlock':
            lock_door(parts[1] if len(parts) > 1 else None, False)
        elif v == 'pos':
            pos = current_position()
            if not pos: print('[position] unavailable')
            else: print(f"[position] track {pos['track']}  index {pos['index']}  {fmt_time(pos['rel_lba']/75)} / {fmt_time(pos['abs_lba']/75)}")
        elif v == 'save':
            save_state(parts[1] if len(parts) > 1 else 'cdplayer-state.json')
            print(f"[save] {parts[1] if len(parts) > 1 else 'cdplayer-state.json'}")
        elif v == 'load':
            path = parts[1] if len(parts) > 1 else 'cdplayer-state.json'
            load_state(path)
        elif v in ('help', 'h', '?'):
            print("""
  play [N]     play drive N (default 0)
  track <N>    jump to track N
  next / n     next track
  prev / b     previous track
  p            pause / resume
  stop         stop playback
  i            disc & track info
  d [N]        measure Dynamic Range (DR14-style)
  hdcd [N]     detect HDCD on the disc or track N
  meta         fetch / re-fetch metadata
  r            rip to FLAC or WAV
  pos          show current position
  eject        eject selected drive
  lock         lock selected drive door
  unlock       unlock selected drive door
  save [file]  save detected-disc state
  load [file]  load saved state
  drives       list drives
  q            quit
""")
        else:
            print(f"[?] unknown: '{v}'.  Type 'help'.")
    except (ValueError, IndexError) as e:
        print(f'[error] {e}')


def main():
    print('cdplayer v1.16.1  -  type \'help\' for commands, \'q\' to quit')
    print('-' * 85)
    print('  Linux Edition - ioctl playback, cdparanoia/ffmpeg ripping')
    print('  Gapless-ready playback  |  DR14-style meter  |  MusicBrainz metadata')
    print('-' * 85)
    print('Scanning for audio CDs …\n')
    threading.Thread(target=_monitor, daemon=True).start()
    time.sleep(1.5)
    while True:
        try:
            cmd = input('>> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            _monitor_stop.set()
            stop_playback()
            sys.exit(0)
        if cmd:
            handle(cmd)

if __name__ == '__main__':
    main()
