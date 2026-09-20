#!/usr/bin/env bash
set -euo pipefail

input=${1:?input video required}
output=${2:?output video required}

ffmpeg -hide_banner -y -i "$input" \
  -filter_complex "[0:v]split=3[base][tree][fire]; \
    [tree]crop=540:960:x='if(lt(t,60.10),0,if(gt(t,62.60),540,540*(t-60.10)/2.50))':y=900,scale=1080:1920:flags=lanczos,format=yuva420p,fade=t=in:st=60.10:d=0.25:alpha=1,fade=t=out:st=62.35:d=0.25:alpha=1[tree_close]; \
    [base][tree_close]overlay=enable='between(t,60.10,62.60)'[with_tree_fix]; \
    [fire]crop=540:960:x=400:y=420,scale=1080:1920:flags=lanczos,format=yuva420p,fade=t=in:st=104.15:d=0.25:alpha=1,fade=t=out:st=105.15:d=0.35:alpha=1[fire_close]; \
    [with_tree_fix][fire_close]overlay=enable='between(t,104.15,105.50)',format=yuv420p[outv]" \
  -map "[outv]" -map 0:a:0 \
  -c:v libx264 -preset medium -crf 17 -profile:v high -level 4.1 \
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 \
  -c:a copy -movflags +faststart "$output"
