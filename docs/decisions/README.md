# Decision records

One file per decision that shaped the speaker: the situation, what was
decided, the evidence, and what it cost. Read the relevant record before
undoing one of these.

New record: copy the shape of an existing one, take the next number, add it
to this table and mention it in the CHANGELOG entry of the change. When a
decision is replaced, set the old record's status to "superseded by NNNN"
rather than deleting it.

| # | Decision | Date | Status |
|---|---|---|---|
| [0001](0001-camilladsp-owns-the-amplifier.md) | CamillaDSP is the only owner of the amplifier | 2026-08-15 | accepted |
| [0002](0002-one-volume-control.md) | One volume control: the PipeWire default sink | 2026-08-18 | accepted |
| [0003](0003-spotifyd.md) | spotifyd as the Spotify Connect receiver | 2026-08-18 | accepted |
| [0004](0004-gemini-live-and-pipecat.md) | Gemini Live (native audio) through pipecat, free tier | 2026-08-21 | accepted |
| [0005](0005-openwakeword.md) | openWakeWord "hey_jarvis" as the wake word | 2026-08-21 | accepted, re-examined 2026-10-08 |
| [0006](0006-hardware-echo-cancellation.md) | Hardware echo cancellation on the XVF3800, with a supervised reference feed | 2026-08-21 | accepted |
| [0007](0007-two-processes.md) | Wake word and conversation in two processes | 2026-08-21 | accepted |
| [0008](0008-half-duplex.md) | Half-duplex conversation instead of voice barge-in | 2026-08-21 | accepted |
| [0009](0009-ducking.md) | Duck music per stream, for the whole session | 2026-08-22 | accepted |
| [0010](0010-voice-dsp-profile-over-websocket.md) | Voice DSP profile over CamillaDSP's websocket, keeping the buffering | 2026-08-21, amended 2026-10-04 | accepted |
| [0011](0011-spotify-web-api-for-search.md) | Spotify Web API for "play X", MPRIS for transport | 2026-08-22 | accepted |
| [0012](0012-registers-at-runtime.md) | XVF3800 settings applied at runtime, never saved to flash | 2026-08-22 | accepted |
| [0013](0013-offline-intents-off.md) | Offline command matching switched off | 2026-10-04 | accepted |
| [0014](0014-mic-channel-1-with-gain.md) | Everything listens on XVF3800 channel 1, with gain for Gemini | 2026-10-04 | accepted |
| [0015](0015-clock-master-and-unresampled-reference.md) | Loopback sink is the clock master; the reference is not resampled | 2026-10-04 | accepted |
| [0016](0016-confirm-then-act.md) | One spoken confirmation, then the action | 2026-10-04 | accepted |
| [0017](0017-household-verifier.md) | A verifier trained on the household's own captures | 2026-10-05 | accepted |
| [0018](0018-second-opinion-blocks.md) | A second detector must confirm; risky tools are guarded | 2026-10-09 | accepted |
| [0019](0019-reply-language.md) | Replies in English unless the request was certainly Russian | 2026-10-04 | accepted |
| [0020](0020-repository-as-source-of-truth.md) | This repository is the source of truth for the device | 2026-10-11 | accepted |
