# Subtitle repair: French

The French language pack for the **subtitle repair of the Yacer build of CoreELEC**
([allolive/CoreELEC](https://github.com/allolive/CoreELEC), branch `yacer`). The repair is part
of that build's Kodi; on any other Kodi or CoreELEC this add-on is installed but does nothing.

## What it does

A subtitle made by OCR from disc images repeats the same misreadings all through it: "l" for
"I", "rn" for "m", "0" for "O", a dropped accent. With this pack, Kodi corrects them in French
subtitles as they are read:

- only a mistake the subtitle repeats, never a one-off;
- only into a French word, and only in lines that are French;
- names, numbers and lines in another language are left as they are.

The file on disk is never changed. The subtitle's language comes from its file name
(`Film.fr.srt`) or the track's language tag; a subtitle that names none is read by a
language detector.

## Where it fits

It is not something to install by hand. In the Yacer build, under **System → Yacer →
Subtitles**:

```
Subtitles
├─ Max brightness
├─ Alter placement
└─ Repair text subtitles            the whole repair, on or off
   ├─ Correct misread letters       the languages to correct: this pack is "French"
   ├─ Correct broken timing
   └─ Remove advertising
```

Ticking **French** under *Correct misread letters* installs this add-on from the allolive
repository; unticking it uninstalls it. Kodi picks the change up without a restart. The setting
decides which packs are kept: a pack installed some other way and not ticked is removed the next
time the list changes. With no language ticked, letters are left alone; the other repairs
(encoding, music notes, timing, advertising) need no pack.

Every ticked language is held in memory while Kodi runs, because each line is weighed against all
of them - tick the languages your subtitles are in, not all of them.

## Contents

- `resources/subtitlerepair/fr/profile.txt` - what the repair needs to know about French
  (its codes, one-letter words, letters and accents), written by `make_pack.py` in
  CoreELEC-yacer's `tests/subtitle-repair`.
- `fr.aff`, `fr.dic` - LibreOffice's French Hunspell dictionary, with `README_dict_fr.txt` (its authors and licence).
  It is not stored in this repository: `dictionaries.txt` pins each file's revision and sha256,
  and the build fetches and checks it.
