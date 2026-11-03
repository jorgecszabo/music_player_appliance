#!/bin/sh
# Route the TWL6040 to both possible audio-out paths and play a test tone.
#   headset path : HS DAC -> HSOL/HSOR
#   line-out path: HF DAC -> HFL/HFR -> AUXL/AUXR switches
# The OMAP4 McPDM link only runs at 88.2/96 kHz, S32_LE. plughw converts
# other rates.
set -x
amixer -c0 -q sset 'Headset Left Playback' 'HS DAC'
amixer -c0 -q sset 'Headset Right Playback' 'HS DAC'
amixer -c0 -q sset 'Headset' 10            # 0..15, about -10 dB
amixer -c0 -q sset 'Handsfree Left Playback' 'HF DAC'
amixer -c0 -q sset 'Handsfree Right Playback' 'HF DAC'
amixer -c0 -q sset 'Handsfree' 15          # 0..29
amixer -c0 -q sset 'AUXL' on
amixer -c0 -q sset 'AUXR' on
amixer -c0 sget 'Headset Power Mode'
# Native rate, then 48 kHz through ALSA's resampler.
speaker-test -D hw:0,0 -c 2 -r 96000 -F S32_LE -t sine -f 440 -l 1
speaker-test -D plughw:0,0 -c 2 -r 48000 -t wav -l 1
