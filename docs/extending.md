# Extending the speaker

How to add things without breaking what took weeks to get right. The workflow
for any change is in [operations.md](operations.md#making-a-change).

## Conventions

- **Name PipeWire nodes, never ids**, and wait for them (`pw_node.wait_for_node`).
- **Play everything into the default (loopback) sink.** Never open
  `hw:sndrpihifiberry`; never route around CamillaDSP. Anything played into the
  loopback sink is automatically processed, volume-controlled and in the
  echo-canceller reference.
- **Never touch the speaker volume except through the default sink**, and do
  not add a second gain stage.
- **Call system binaries by absolute path** (`/usr/bin/wpctl`, `/usr/bin/pw-cat`):
  the services run with a minimal environment.
- **Ship risky behaviour behind a switch, off or log-only first.** The wake
  word's second opinion went `log` for a day, was compared against what
  actually happened, and only then `block`. Do the same.
- **Journal outcomes, not only errors.** Most fixes here were found by reading
  what the services had logged (scores, tool calls, verdicts).
- **XVF3800 registers are applied at start, never saved to flash**, and never
  read if write-only.
- **Every setting has a default in code and, if it differs, a commented line
  in the unit** saying why it has that value.

## Add a voice tool

1. Write an async function taking `params: FunctionCallParams` and typed
   arguments, with a docstring that tells the model when to use it. pipecat
   builds the schema from the signature and docstring. Put it in an existing
   `*_tools.py` module or a new one in `src/`.
2. For a one-shot action, finish with
   `await session_signal.complete_quick_action(params, {"status": "ok", …}, then=...)`.
   Put the side effect that should happen *after* the spoken confirmation
   (starting audio, for example) in `then`. Otherwise call
   `await params.result_callback({...})`.
3. Register it in `voice_agent.py`: import it and add it to the `tools=[...]`
   list in `_run_session`.
4. If a false wake-up could trigger it harmfully, add its name to
   `VOICE_TOOL_GUARD` in `voice-agent.service`.
5. If the model would otherwise refuse ("I can't set timers"), mention the
   capability in `VOICE_SYSTEM_PROMPT`.
6. Test without the speaker: `tools/session/voice-session-replay.py` runs the
   real session code on a recording with the speaker, LEDs, CamillaDSP and
   tools stubbed, against real Gemini. Add a unit test in `tests/` for any
   parsing.
7. Update [voice-agent.md](voice-agent.md#tools) and the CHANGELOG.

The offline intent path (`local_intent.py`) is off; a tool only needs to exist
on the Gemini side.

## Add a service

1. Program in `src/` (it is installed to `~/.local/bin`), unit in
   `user/.config/systemd/user/<name>.service`, with `Restart=on-failure`,
   `NoNewPrivileges=true`, `WantedBy=default.target`, and `After=`/`Wants=` on
   `pipewire-pulse.service` if it uses audio. The manifest's globs pick both
   up.
2. Needs its own Python packages? Create a venv and a
   `deps/requirements-<name>.txt`, and add it to `cmd_bootstrap_user` in
   `install.sh`.
3. Add it to the enable list in `cmd_bootstrap_user` and to the unit list in
   `ai-speaker-status.sh`.
4. Talking to other services: a UNIX socket in `$XDG_RUNTIME_DIR`, newline-
   delimited text, like the existing ones ([architecture.md](architecture.md#how-the-processes-talk)).
5. Document it in [architecture.md](architecture.md#services).

## Add a setting

Read it in code with a default (`os.environ.get("NAME", "default")`), set it in
the unit with a comment if the value differs from the default, and add a row
to [configuration.md](configuration.md).

## Add a media source

Play into the default sink through PipeWire (PulseAudio or native). For it to
be ducked during voice sessions, make `VOICE_DUCK_MATCH` match its
`application.name` or `application.process.binary` (`python3 ~/.local/bin/duck.py`
shows what would be ducked). For buttons and the Spotify-style volume sync it
needs an MPRIS interface.

## Add or change a DSP profile

1. Copy `system/etc/camilladsp/profiles/music.yml`. Keep the mono split, the
   high-pass on the woofer, the crossover, the tweeter trim and the clip guard:
   **every profile must protect the tweeter.**
2. Keep `devices` identical to `music.yml` unless you intend to re-measure the
   echo-canceller timing (CamillaDSP's buffering is part of the delay the
   canceller is aligned to).
3. `camilladsp --check file.yml`, then `sudo ./install.sh apply system`, then
   try it with `sudo smartspk-profile <name>` or in CamillaGUI.

## Change the voice engine or model

Model: `VOICE_MODEL` in `voice-agent.service`. Another provider: add a builder
in `voice_engine.py` that returns the pipecat service and its defaults, and
select it with `VOICE_ENGINE`. Check that it supports function calling, and
measure the time from trigger to ready: held audio is only sent once the engine
is ready.

## Testing audio changes

Offline tests have passed code that then failed on the device. Before
installing anything that opens streams, changes timing or touches WirePlumber:

- Run the new program **as a second instance** next to the live one, with its
  own socket paths, health file and capture directory under
  `$XDG_RUNTIME_DIR`, while other streams start and stop. Never use the live
  `wake-trigger.sock`.
- `voice-session-replay.py session --real-mic` runs the session code on the
  real microphone with output and tools stubbed.
- Give measurement scripts a `--dry-run` that plays digital silence and
  touches no register, and run it.
- After WirePlumber changes: `ai-speaker-status.sh`, then measure the echo
  canceller (`tools/aec/aec-ref-timing-probe.py lead`, then
  `aec-align-check.py`).
- Say plainly which parts were exercised live and which were not.

## Known next steps

From the open items: a custom wake-word model trained on this voice; explicit
speech start/end for Gemini to fix far-field commands over music; keeping the
XVF3800 sink running through pauses so the canceller does not re-converge each
time music starts; a Spotify Web API fallback for play/pause/next when no
session exists; Home Assistant control.
