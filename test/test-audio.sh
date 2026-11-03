#!/bin/sh
# Try to route the TWL6040 to the 3.5 mm headset jack and play a test tone.
# Control names come from the mainline twl6040 codec driver; print all of
# them with `amixer -c0 controls` if something here fails.
set -x
amixer -c0 controls | head -60
amixer -c0 sset 'Headset Left Playback' 'HS DAC' 2>/dev/null
amixer -c0 sset 'Headset Right Playback' 'HS DAC' 2>/dev/null
amixer -c0 sset 'Headset' 70% unmute 2>/dev/null
amixer -c0 sset 'DL1 PDM' on 2>/dev/null
amixer -c0 sset 'DL1 PDM Switch' on 2>/dev/null
speaker-test -D hw:0,0 -c 2 -r 48000 -F S32_LE -t sine -f 440 -l 2
