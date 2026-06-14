# Media-details helper for the skin's "Media info" popup + OSD language names.
#
# Library mode (RunScript(...,<dbid>,<dbtype>)): pulls streamdetails over
#   JSON-RPC, maps codes->friendly names, and stashes them in Home window
#   properties the popup displays.
# Player mode  (RunScript(...,player)): maps the currently playing audio/
#   subtitle language for the OSD labels.

import json
import sys

import xbmc
import xbmcgui

HOME = xbmcgui.Window(10000)
MAX_AUDIO = 16  # matches the number of audio rows the popup renders

# audio codec id (as stored in streamdetails) -> friendly name.
# Plain 1:1 lookup; unknown codecs fall back to the raw id upper-cased.
CODEC = {
    "ac3": "Dolby Digital",
    "eac3": "Dolby Digital+", "ec3": "Dolby Digital+",
    "eac3_atmos": "Dolby Digital+ Atmos", "ec3_atmos": "Dolby Digital+ Atmos",
    "eac3_ddp_atmos": "Dolby Digital+ Atmos", "ddp_atmos": "Dolby Digital+ Atmos",
    "truehd": "Dolby TrueHD", "mlp": "Dolby TrueHD",
    "truehd_atmos": "Dolby TrueHD Atmos",
    "ac4": "Dolby AC-4",
    "dts": "DTS", "dca": "DTS",
    "dtshd": "DTS-HD", "dtshd_ma": "DTS-HD MA", "dtshd_hra": "DTS-HD HR",
    "dtsx": "DTS:X", "dtshd_ma_x": "DTS:X", "dtshd_ma_x_imax": "DTS:X IMAX",
    "dtshd_ma_imax": "DTS-HD MA",
    "aac": "AAC", "aac_latm": "AAC", "heaac": "HE-AAC",
    "flac": "FLAC", "alac": "ALAC", "opus": "Opus", "vorbis": "Vorbis",
    "mp3": "MP3", "mp2": "MP2",
    "wmav2": "WMA", "wmapro": "WMA Pro",
    "pcm": "PCM", "lpcm": "PCM",
    "pcm_s16le": "PCM", "pcm_s24le": "PCM", "pcm_s32le": "PCM",
    "pcm_bluray": "PCM", "pcm_dvd": "PCM",
}
# channel count -> layout
CH = {"1": "1.0", "2": "2.0", "3": "2.1", "4": "4.0", "5": "5.0",
      "6": "5.1", "7": "6.1", "8": "7.1", "10": "9.1"}

METHODS = {
    "movie": ("VideoLibrary.GetMovieDetails", "movieid", "moviedetails"),
    "episode": ("VideoLibrary.GetEpisodeDetails", "episodeid", "episodedetails"),
    "musicvideo": ("VideoLibrary.GetMusicVideoDetails", "musicvideoid",
                   "musicvideodetails"),
}


def jsonrpc(method, params):
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        return json.loads(xbmc.executeJSONRPC(json.dumps(req))).get("result", {})
    except (ValueError, TypeError):
        return {}


def friendly_codec(c):
    return CODEC.get((c or "").lower().strip(), (c or "").upper())


def lang_name(code):
    code = (code or "").strip()
    if not code:
        return ""
    return xbmc.convertLanguage(code, xbmc.ENGLISH_NAME) or code.upper()


mode = sys.argv[1].strip() if len(sys.argv) > 1 else ""

# ---- OSD/player mode: map the currently playing audio/subtitle language ----
if mode == "player":
    players = jsonrpc("Player.GetActivePlayers", {}) or []
    pid = next((p.get("playerid") for p in players if p.get("type") == "video"),
               players[0].get("playerid") if players else None)
    props = jsonrpc("Player.GetProperties",
                    {"playerid": pid,
                     "properties": ["currentaudiostream", "currentsubtitle"]}) \
        if pid is not None else {}
    HOME.setProperty("MediaInfo.Player.AudioLanguage",
                     lang_name((props.get("currentaudiostream") or {}).get("language", "")))
    HOME.setProperty("MediaInfo.Player.SubtitleLanguage",
                     lang_name((props.get("currentsubtitle") or {}).get("language", "")))
    sys.exit()

# ---- library mode: per-stream friendly properties for the popup ----
dbid = mode or xbmc.getInfoLabel("ListItem.DBID").strip()
dbtype = (sys.argv[2].strip() if len(sys.argv) > 2 else "") \
    or xbmc.getInfoLabel("ListItem.DBType").strip()

sd = {}
if dbid and dbtype in METHODS:
    method, idkey, detkey = METHODS[dbtype]
    res = jsonrpc(method, {idkey: int(dbid), "properties": ["streamdetails"]})
    sd = res.get(detkey, {}).get("streamdetails", {}) or {}

audio = sd.get("audio", []) or []
for i, a in enumerate(audio[:MAX_AUDIO], start=1):
    codec = a.get("codec", "") or ""
    ch = str(a.get("channels", "") or "")
    HOME.setProperty("MediaInfo.Audio.%d.Codec" % i,
                     friendly_codec(codec) if codec else "")
    HOME.setProperty("MediaInfo.Audio.%d.Channels" % i, CH.get(ch, ch))
    HOME.setProperty("MediaInfo.Audio.%d.Language" % i,
                     lang_name(a.get("language", "")))
for m in range(len(audio) + 1, MAX_AUDIO + 1):
    HOME.clearProperty("MediaInfo.Audio.%d.Codec" % m)
    HOME.clearProperty("MediaInfo.Audio.%d.Channels" % m)
    HOME.clearProperty("MediaInfo.Audio.%d.Language" % m)

seen = []
for s in sd.get("subtitle", []) or []:
    name = lang_name(s.get("language", ""))
    if name and name not in seen:
        seen.append(name)
HOME.setProperty("MediaInfo.Subtitles", ", ".join(seen))
