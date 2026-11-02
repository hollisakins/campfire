#!/bin/zsh
# Draw the NIRSpec docs figures from an ember_cosmos_p1 reduction.
# Usage: zsh scripts/docs/nirspec/figures_p1.sh <CAMPFIRE_ROOT>
# The exposures and sources below are the examples chosen for p1; to use another
# observation, copy this file and change them.

ROOT=${1:?usage: figures_p1.sh <CAMPFIRE_ROOT>}
D=${0:A:h}
OBS=ember_cosmos_p1
PY=(conda run --no-capture-output -n campfire python)

$PY $D/fig_overlap.py --root $ROOT --obs $OBS --exposure jw07076001001_05101_00001_nrs1
$PY $D/fig_master_background.py --root $ROOT --obs $OBS --exposure jw07076001001_03101_00001_nrs1
$PY $D/fig_bar_shadow.py --root $ROOT --disperser prism
$PY $D/fig_stuck_shutters.py --root $ROOT --obs $OBS --example jw07076001001_03101_00001_nrs1:q4r58c92 \
    --example jw07076001001_05101_00002_nrs1:q3r9c19
$PY $D/fig_failed_open.py --root $ROOT --obs $OBS --exposure jw07076001001_03101_00001_nrs1 \
    --shutters 11 19 12 --labels "all of it" "about half" "almost none"
$PY $D/fig_nod_subtraction.py --root $ROOT --obs $OBS --source 634072 \
    --exposure-group jw07076001001_03101 --detector nrs2
$PY $D/fig_nod_subtraction.py --root $ROOT --obs $OBS --source 925404 \
    --exposure-group jw07076001001_03101 --detector nrs1 --mark --out nod_left_out.png
