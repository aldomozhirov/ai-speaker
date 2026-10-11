# No longer parked: installed 2026-10-04 23:00

The design in this folder (shared `clock.name` for the two sinks, 1792 frames
of headroom on the XVF3800 sink, `XVF_AEC_REF_DELAY=0`) is live, together
with `priority.driver = 2500` for the program sink, which this folder's copy
of `51-smart-speaker.conf` does not have. The live files are the reference;
the copies here are the versions tried at run time between 21:00 and 21:30.

Why it came back: realtime priority (in place since the 21:49 reboot) did not
stop the reference from sliding, because the slides come from the rate
matching itself, which chases snd-aloop's uneven clock. Realtime priority
does cover this design's weakness, a late `pw-loopback` leaving a 5.3 ms
step: 0 steps in 170 s under the synthetic load.

See "After the reboot" in `../WAKE-WORD-RELIABILITY-FOLLOWUP-2026-10-04.md`.
To go back to the rate-matched reference, restore
`~/.config/wireplumber/wireplumber.conf.d/51-smart-speaker.conf.bak-rate-matched-reference`
and `~/.config/systemd/user/xvf-aec-ref.service.bak-delay-0.04-rate-matched`,
then `systemctl --user daemon-reload`, restart wireplumber, restart
xvf-aec-ref.
