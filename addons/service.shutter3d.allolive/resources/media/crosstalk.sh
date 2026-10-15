#!/bin/sh
# Makes the add-on's test patterns, crosstalk-<fps>.3D.SBS.mkv for 23.976, 24,
# 25, 29.97 and 30 fps: 10 minutes of side by side 3D each, which the box runs
# at four times the rate (95.9, 96, 100, 119.9 and 120 Hz) as it does a film.
# Each eye is drawn at 1920x1080 and squeezed into its half. A white bar
# bounces between the edges of the screen, 18 px a frame (one crossing every
# 104 frames), the two eyes' bars going opposite ways, with each eye's name
# and the rate where the other eye has none.
# Whole-pixel steps and one keyframe every 2400 frames keep them small.
# Usage: crosstalk.sh [directory] [fps...]   (all of them by default)
set -eu
font=$(fc-match -f '%{file}' 'DejaVu Sans:bold')
eye() { # <bar x> <label> <label x> <label y>
  printf "overlay=x='%s':y=0:eval=frame:shortest=1,drawtext=fontfile=%s:text='%s':fontcolor=white:fontsize=90:x=%s:y=%s,scale=960:1080:flags=neighbor" \
    "$1" "$font" "$2" "$3" "$4"
}
pattern() { # <fps> <rate>
  ffmpeg -hide_banner -loglevel error -y \
    -f lavfi -i "color=black:s=1920x1080:r=$2:d=600" \
    -f lavfi -i "color=white:s=48x1080:r=$2" \
    -filter_complex "[1]split[b1][b2];[0]split[k1][k2];
      [k1][b1]$(eye '18*(104-abs(mod(n\,208)-104))' "LEFT EYE  $1" 120 120)[l];
      [k2][b2]$(eye '1872-18*(104-abs(mod(n\,208)-104))' "RIGHT EYE  $1" 'w-tw-120' 'h-th-160')[r];
      [l][r]hstack,format=yuv420p" \
    -c:v libx264 -preset veryslow -tune animation -crf 20 -g 2400 \
    -metadata:s:v:0 stereo_mode=left_right -metadata title="Shutter 3D crosstalk pattern $1 fps" \
    "$dir/crosstalk-$1.3D.SBS.mkv"
}
dir=${1:-.}; [ $# -gt 0 ] && shift
[ $# -gt 0 ] || set -- 23.976 24 25 29.97 30
for fps; do
  case $fps in
    23.976) pattern 23.976 24000/1001 ;;
    29.97) pattern 29.97 30000/1001 ;;
    24|25|30) pattern "$fps" "$fps" ;;
    *) echo "no pattern for $fps fps" >&2; exit 1 ;;
  esac
done
