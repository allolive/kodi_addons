#!/usr/bin/env python3
"""Consistency check for a skin directory: things Kodi resolves by name at runtime and
silently ignores when missing or defined twice.

  check-skin.py <skin-dir> [<baseline-skin-dir>]

Reports variables, includes, strings (skin range 31000-39999), fonts and textures that are
referenced but not defined, and variables, includes, strings, fonts and colours defined twice.
With a baseline (e.g. the unpatched upstream skin), problems the baseline already has are
not reported. Exit status 1 when anything new is found.
"""
import os
import re
import sys
from collections import Counter

def load(d):
    xml = {}
    for root, _, files in os.walk(os.path.join(d, 'xml')):
        for f in files:
            if f.endswith('.xml'):
                xml[f] = open(os.path.join(root, f), encoding='utf-8').read()
    return xml

def media_files(d):
    out = set()
    for sub in ('media', 'themes'):
        base = os.path.join(d, sub)
        for root, _, files in os.walk(base):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), base)
                out.add(rel.split(os.sep, 1)[1] if sub == 'themes' else rel)
    return {m.lower() for m in out}

def problems(d):
    xml = load(d)
    alltext = '\n'.join(xml.values())
    out = set()

    var_defs = Counter(re.findall(r'<variable\s+name="([^"]+)"', alltext))
    inc_defs = Counter(re.findall(r'<include\s+name="([^"]+)"', alltext))
    for n, c in var_defs.items():
        if c > 1:
            out.add(('variable defined twice', n))
    for n, c in inc_defs.items():
        if c > 1:
            out.add(('include defined twice', n))
    for n in set(re.findall(r'\$VAR\[([^\],]+)', alltext)):
        if n not in var_defs:
            out.add(('undefined variable', n))
    used_inc = ({b.strip() for b in re.findall(r'<include\b[^>]*>([^<]*)</include>', alltext)}
                | set(re.findall(r'<include\s+content="([^"]+)"', alltext)))
    for n in used_inc:
        if n and '$' not in n and n not in inc_defs:
            out.add(('undefined include', n))

    po = os.path.join(d, 'language', 'resource.language.en_gb', 'strings.po')
    ids = Counter(int(i) for i in re.findall(r'^msgctxt "#(\d+)"', open(po, encoding='utf-8').read(), re.M))
    for i, c in ids.items():
        if c > 1:
            out.add(('string defined twice', str(i)))
    for i in set(int(x) for x in re.findall(r'(?:\$LOCALIZE\[|<label>|<label2>|idloc=")(3\d{4})\b', alltext)):
        if i not in ids:
            out.add(('undefined string', str(i)))

    fonts = xml.get('Font.xml', '')
    for fs in re.findall(r'<fontset\b.*?</fontset>', fonts, re.S):
        fs_id = re.search(r'id="([^"]+)"', fs).group(1)
        names = Counter(re.findall(r'<name>([^<]+)</name>', fs))
        for n, c in names.items():
            if c > 1:
                out.add(('font defined twice', f'{fs_id}/{n}'))
        for f in set(re.findall(r'<filename>([^<]+)</filename>', fs)):
            if not os.path.exists(os.path.join(d, 'fonts', f)) and f != 'arial.ttf':
                out.add(('missing font file', f))
    default = re.search(r'<fontset\b[^>]*id="Default".*?</fontset>', fonts, re.S)
    default_names = set(re.findall(r'<name>([^<]+)</name>', default.group(0))) if default else set()
    for n in set(re.findall(r'<font>([^<$]+)</font>', alltext)):
        if n not in default_names:
            out.add(('font not in Default fontset', n))

    for f in os.listdir(os.path.join(d, 'colors')):
        c = Counter(re.findall(r'<color\s+name="([^"]+)"', open(os.path.join(d, 'colors', f), encoding='utf-8').read()))
        for n, k in c.items():
            if k > 1:
                out.add(('colour defined twice', f'{f}/{n}'))

    media = media_files(d)
    if media:
        for t in set(re.findall(r'>([^<>$\[\]]+\.(?:png|jpg|gif))<', alltext)) | set(re.findall(r'value="([^"$\[\]]+\.(?:png|jpg|gif))"', alltext)):
            t = t.strip()
            if '://' in t or t.startswith('special:'):
                continue
            if t.lower() not in media:
                out.add(('missing texture', t))
    return out

new = problems(sys.argv[1])
if len(sys.argv) > 2:
    new -= problems(sys.argv[2])
for kind, name in sorted(new):
    print(f'{kind}: {name}')
sys.exit(1 if new else 0)
