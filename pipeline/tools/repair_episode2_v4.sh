#!/usr/bin/env bash
set -euo pipefail

input=${1:?input video required}
output=${2:?output video required}

# Calm the 01:35–01:44 camp passage without regenerating imagery.  A natural
# close-up on the hunter and fire hides the wildcat's discontinuous jump and
# excessive generated motion while the original ambience remains untouched.
ffmpeg -hide_banner -y -i "$input" \
  -filter_complex "[0:v]split=2[base][calm]; \
    [calm]crop=540:960:x=540:y=300,scale=1080:1920:flags=lanczos,format=yuva420p,fade=t=in:st=94.80:d=0.35:alpha=1,fade=t=out:st=104.15:d=0.35:alpha=1[calm_close]; \
    [base][calm_close]overlay=enable='between(t,94.80,104.50)',format=yuv420p[outv]" \
  -map "[outv]" -map 0:a:0 \
  -c:v libx264 -preset medium -crf 17 -profile:v high -level 4.1 \
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 \
  -c:a copy -movflags +faststart "$output"
