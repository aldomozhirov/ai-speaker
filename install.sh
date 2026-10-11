#!/usr/bin/env bash
# Install, compare or capture the AI speaker's files. See docs/operations.md.
#
#   ./install.sh diff    [user|system|all] [--show]   repo vs. live, changes nothing
#   ./install.sh apply   [user|system|all] [--restart] copy changed files repo -> live
#   ./install.sh capture [user|system|all]            copy live -> repo (adopt edits made on the device)
#   ./install.sh bootstrap-system                     fresh Pi, part 1 (run with sudo)
#   ./install.sh bootstrap-user                       fresh Pi, part 2 (run as the speaker's user)
#
# "system" files are root-owned: run those commands with sudo. The speaker's
# login user is then taken from SUDO_USER (or TARGET_USER).
#
# Every file is rendered for the target machine on the way in: the reference
# unit's home directory, user id and XVF3800 serial (REF_* below) become the
# target's. `capture` does the reverse. On the reference unit both are no-ops.
set -euo pipefail

REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
MANIFEST=$REPO/manifest.txt

# The unit these files were captured from.
REF_HOME=/home/aldom
REF_UID=1000
REF_XVF_SERIAL=114993700254500350

# Pinned third-party versions (bootstrap only).
CAMILLADSP_VERSION=4.1.3
CAMILLADSP_SHA256=d9a17092923ebfe5d20a770c6b6a7eb2268f9700f999bf604b9db09f518aca5a
CAMILLAGUI_VERSION=4.1.0
CAMILLAGUI_SHA256=9a5415b44dda58478f18de9fd572edf092f659fd5e45cbe8086ff5648dc089d7
SPOTIFYD_VERSION=v0.4.2
XVF_REPO=https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY.git
XVF_REPO_COMMIT=a652fe79da3a292b25decc0e1e7f267d29bb0284
VOSK_MODEL=vosk-model-small-en-us-0.15

die()  { echo "ERROR: $*" >&2; exit 1; }
note() { echo "==> $*"; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# --- target identity -----------------------------------------------------------

TARGET_USER=${TARGET_USER:-${SUDO_USER:-$(id -un)}}
[ "$TARGET_USER" != root ] || die "Set TARGET_USER to the speaker's login user."
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
TARGET_UID=$(id -u "$TARGET_USER")
TARGET_GID=$(id -g "$TARGET_USER")

detect_xvf_serial() {
  local d
  for d in /sys/bus/usb/devices/*; do
    if [ "$(cat "$d/idVendor" 2>/dev/null)" = 2886 ] && [ "$(cat "$d/idProduct" 2>/dev/null)" = 001a ]; then
      cat "$d/serial"; return 0
    fi
  done
  return 1
}
XVF_SERIAL=${XVF_SERIAL:-$(detect_xvf_serial || echo "$REF_XVF_SERIAL")}

# --- rendering -----------------------------------------------------------------

is_text() { [ ! -s "$1" ] || grep -Iq . "$1"; }

# render SRC DST: repo -> target values
render() {
  if is_text "$1"; then
    sed -e "s|$REF_HOME|$TARGET_HOME|g" \
        -e "s|/run/user/$REF_UID|/run/user/$TARGET_UID|g" \
        -e "s|$REF_XVF_SERIAL|$XVF_SERIAL|g" "$1" > "$2"
  else
    cp "$1" "$2"
  fi
}

# unrender SRC DST: target values -> repo
unrender() {
  if is_text "$1"; then
    sed -e "s|$TARGET_HOME|$REF_HOME|g" \
        -e "s|/run/user/$TARGET_UID|/run/user/$REF_UID|g" \
        -e "s|$XVF_SERIAL|$REF_XVF_SERIAL|g" "$1" > "$2"
  else
    cp "$1" "$2"
  fi
}

# --- manifest ------------------------------------------------------------------

# Prints "scope<TAB>repo-file<TAB>live-file" for every tracked file.
# With "live", the patterns are expanded on the device instead of in the
# repo, so files the repo does not have yet show up too (capture, UNTRACKED).
entries() {
  local side=${1:-repo} scope pattern dest f live base dir
  while read -r scope pattern dest; do
    case "$scope" in ''|\#*) continue ;; esac
    dest=${dest/#\~/$TARGET_HOME}
    if [ "$side" = live ]; then
      if [ "${dest%/}" = "$dest" ]; then
        [ -e "$dest" ] && printf '%s\t%s\t%s\n' "$scope" "$pattern" "$dest"
        continue
      fi
      dir=${pattern%/*}; base=${pattern##*/}
      for live in "$dest"$base; do
        [ -e "$live" ] || continue
        case "$live" in *.bak*|*.orig|*.proposed*) continue ;; esac
        printf '%s\t%s\t%s\n' "$scope" "$dir/${live##*/}" "$live"
      done
      continue
    fi
    for f in "$REPO"/$pattern; do
      [ -e "$f" ] || continue
      f=${f#"$REPO"/}
      if [ "${dest%/}" != "$dest" ]; then live=$dest${f##*/}; else live=$dest; fi
      printf '%s\t%s\t%s\n' "$scope" "$f" "$live"
    done
  done < "$MANIFEST"
}

want_scope() { [ "$1" = all ] || [ "$1" = "$2" ]; }

need_root_for() {
  if [ "$1" != user ] && [ "$(id -u)" -ne 0 ]; then
    die "'$1' includes root-owned files: run with sudo (or pass 'user')."
  fi
}

# Owner/group/mode for a live path.
ownership() {
  local live=$1 repo=$2 mode
  if [ -x "$REPO/$repo" ]; then mode=0755; else mode=0644; fi
  case "$live" in
    /etc/camilladsp/profiles/*) echo "root camillaconfig 0660" ;;
    /etc/camillagui/*)          echo "root camillaconfig 0640" ;;
    /etc/*|/usr/*)              echo "root root $mode" ;;
    *)                          echo "$TARGET_UID $TARGET_GID $mode" ;;
  esac
}

# --- commands --------------------------------------------------------------------

cmd_diff() {
  local scope=${1:-all} show=${2:-} tmp=$WORK/render s repo live n_same=0 n_diff=0
  while IFS=$'\t' read -r s repo live; do
    want_scope "$scope" "$s" || continue
    if [ ! -e "$live" ] && [ ! -x "$(dirname "$live")" ]; then
      echo "NO-ACCESS $live  (run with sudo)"; continue
    fi
    if [ ! -e "$live" ]; then
      echo "MISSING   $live"; n_diff=$((n_diff+1)); continue
    fi
    if [ ! -r "$live" ]; then
      echo "NO-ACCESS $live  (run with sudo)"; continue
    fi
    render "$REPO/$repo" "$tmp"
    if cmp -s "$tmp" "$live"; then
      n_same=$((n_same+1))
    else
      echo "DIFFERS   $live"; n_diff=$((n_diff+1))
      [ "$show" = --show ] && diff -u --label "live:$live" --label "repo:$repo" "$live" "$tmp" || true
    fi
  done < <(entries repo)
  # Files on the device that the repo does not have yet.
  while IFS=$'\t' read -r s repo live; do
    want_scope "$scope" "$s" || continue
    [ -e "$REPO/$repo" ] || { echo "UNTRACKED $live  (./install.sh capture adds it)"; n_diff=$((n_diff+1)); }
  done < <(entries live)
  echo "$n_same identical, $n_diff to look at."
  [ "$n_diff" -eq 0 ]
}

cmd_apply() {
  local scope=${1:-all} restart=${2:-} tmp=$WORK/render s repo live owner group mode stamp backup changed=()
  need_root_for "$scope"
  stamp=$(date +%Y%m%d-%H%M%S)
  backup=$TARGET_HOME/.local/state/ai-speaker/backups/$stamp
  while IFS=$'\t' read -r s repo live; do
    want_scope "$scope" "$s" || continue
    render "$REPO/$repo" "$tmp"
    if [ -e "$live" ] && cmp -s "$tmp" "$live"; then continue; fi
    if [ -e "$live" ]; then
      install -d "$backup$(dirname "$live")"
      cp -p "$live" "$backup$live"
    fi
    read -r owner group mode < <(ownership "$live" "$repo")
    install -d "$(dirname "$live")"
    install -o "$owner" -g "$group" -m "$mode" "$tmp" "$live"
    echo "installed $live"
    changed+=("$live")
  done < <(entries repo)
  if [ -d "$backup" ]; then
    chown -R "$TARGET_UID:$TARGET_GID" "$TARGET_HOME/.local/state/ai-speaker" 2>/dev/null || true
    echo "previous versions saved under $backup"
  fi
  [ ${#changed[@]} -gt 0 ] || { echo "nothing to change"; return 0; }
  report_restarts "$restart" "${changed[@]}"
}

# Which units does a set of changed files affect? A unit file itself, any unit
# whose ExecStart names the file, and (for Python modules) any unit whose main
# script imports it. Restarts only with --restart; WirePlumber and system
# units are always left to the operator.
report_restarts() {
  local restart=$1; shift
  local f unit units=() mod u system_changed=0 wp_changed=0
  local unit_dir=$TARGET_HOME/.config/systemd/user
  for f in "$@"; do
    case "$f" in
      "$unit_dir"/*.service) units+=("$(basename "$f")") ;;
      */wireplumber/*) wp_changed=1 ;;
      /etc/*|/usr/*) system_changed=1 ;;
      *)
        mod=$(basename "$f"); mod=${mod%.py}
        for u in "$unit_dir"/*.service; do
          if grep -q -- "$(basename "$f")" "$u" || { [ "${f%.py}" != "$f" ] && main_imports "$u" "$mod"; }; then
            units+=("$(basename "$u")")
          fi
        done ;;
    esac
  done
  mapfile -t units < <(printf '%s\n' "${units[@]}" | sort -u | sed '/^$/d')
  [ "$system_changed" -eq 1 ] && echo "system files changed: sudo systemctl daemon-reload, then restart what they belong to (camilladsp, camillagui, udev rules need a replug)."
  [ "$wp_changed" -eq 1 ] && echo "WirePlumber config changed: systemctl --user restart wireplumber, then run ai-speaker-status.sh (clock master!)."
  [ ${#units[@]} -gt 0 ] || return 0
  if [ "$restart" = --restart ]; then
    as_user systemctl --user daemon-reload
    as_user systemctl --user restart "${units[@]}"
    echo "restarted: ${units[*]}"
  else
    echo "to take effect: systemctl --user daemon-reload && systemctl --user restart ${units[*]}"
  fi
}

# Does the unit's main script import module $2 (directly)?
main_imports() {
  local script
  script=$(sed -n 's/^ExecStart=.* \(%h\|[^ ]*\)\/\.local\/bin\/\([^ ]*\.py\).*/\2/p' "$1" | head -1)
  [ -n "$script" ] && grep -Eq "^(from|import) $2( |$|,)" "$TARGET_HOME/.local/bin/$script"
}

as_user() {
  if [ "$(id -u)" -eq 0 ]; then
    runuser -u "$TARGET_USER" -- env XDG_RUNTIME_DIR=/run/user/$TARGET_UID \
      DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$TARGET_UID/bus "$@"
  else
    "$@"
  fi
}

cmd_capture() {
  local scope=${1:-all} s repo live n=0
  need_root_for "$scope"
  while IFS=$'\t' read -r s repo live; do
    want_scope "$scope" "$s" || continue
    [ -r "$live" ] || { echo "NO-ACCESS $live  (run with sudo)"; continue; }
    mkdir -p "$REPO/$(dirname "$repo")"
    unrender "$live" "$REPO/$repo.tmp"
    if [ -e "$REPO/$repo" ] && cmp -s "$REPO/$repo.tmp" "$REPO/$repo"; then
      rm -f "$REPO/$repo.tmp"; continue
    fi
    mv "$REPO/$repo.tmp" "$REPO/$repo"
    if [ -x "$live" ]; then chmod 0755 "$REPO/$repo"; else chmod 0644 "$REPO/$repo"; fi
    [ "$(id -u)" -eq 0 ] && chown "$TARGET_UID:$TARGET_GID" "$REPO/$repo"
    echo "captured $live -> $repo"; n=$((n+1))
  done < <(entries live)
  echo "$n file(s) captured. Review with: git -C '$REPO' diff"
}

# --- fresh Pi ----------------------------------------------------------------------

cmd_bootstrap_system() {
  [ "$(id -u)" -eq 0 ] || die "Run with sudo."
  [ "$(uname -m)" = aarch64 ] || die "64-bit Raspberry Pi OS required."
  local work=$WORK

  note "apt packages"
  apt-get update
  # shellcheck disable=SC2046
  DEBIAN_FRONTEND=noninteractive apt-get install -y $(sed 's/#.*//' "$REPO/deps/apt-packages.txt")

  note "boot configuration"
  grep -q '^dtoverlay=hifiberry-dacplus-std' /boot/firmware/config.txt \
    || echo "WARNING: add the lines in system/boot/firmware/config.txt.fragment to /boot/firmware/config.txt and reboot."

  note "CamillaDSP $CAMILLADSP_VERSION"
  if ! /usr/local/bin/camilladsp --version 2>/dev/null | grep -q "$CAMILLADSP_VERSION"; then
    curl -fL -o "$work/cdsp.tgz" "https://github.com/HEnquist/camilladsp/releases/download/v$CAMILLADSP_VERSION/camilladsp-linux-aarch64.tar.gz"
    echo "$CAMILLADSP_SHA256  $work/cdsp.tgz" | sha256sum -c -
    tar -xzf "$work/cdsp.tgz" -C "$work"
    install -m 0755 "$work/camilladsp" /usr/local/bin/camilladsp
  fi

  note "CamillaGUI $CAMILLAGUI_VERSION"
  if [ ! -x /opt/camillagui_backend/camillagui_backend ]; then
    curl -fL -o "$work/gui.tgz" "https://github.com/HEnquist/camillagui-backend/releases/download/v$CAMILLAGUI_VERSION/bundle_linux_aarch64.tar.gz"
    echo "$CAMILLAGUI_SHA256  $work/gui.tgz" | sha256sum -c -
    mkdir -p "$work/gui" && tar -xzf "$work/gui.tgz" -C "$work/gui"
    rm -rf /opt/camillagui_backend && mv "$work/gui/camillagui_backend" /opt/camillagui_backend
    chown -R root:root /opt/camillagui_backend
  fi

  note "service accounts and directories"
  getent group camillaconfig >/dev/null || groupadd --system camillaconfig
  id camilladsp >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin camilladsp
  usermod -aG audio,camillaconfig camilladsp
  id camillagui >/dev/null 2>&1 || useradd --system --gid camillaconfig --home-dir /nonexistent --shell /usr/sbin/nologin camillagui
  install -d -o root -g camillaconfig -m 2750 /etc/camilladsp /etc/camillagui /etc/camilladsp/backups
  install -d -o root -g camillaconfig -m 2770 /etc/camilladsp/profiles /etc/camilladsp/coeffs

  note "system files"
  cmd_apply system

  [ -e /etc/camilladsp/active.yml ] || {
    ln -sfn /etc/camilladsp/profiles/music.yml /etc/camilladsp/profiles/.active.yml
    ln -sfn /etc/camilladsp/profiles/.active.yml /etc/camilladsp/active.yml
  }

  note "user $TARGET_USER: groups, lingering"
  # pipewire: realtime priority for the audio threads (limits.d/25-pw-rlimits.conf ships with PipeWire)
  getent group pipewire >/dev/null || groupadd --system pipewire
  usermod -aG audio,plugdev,pipewire,gpio "$TARGET_USER"
  loginctl enable-linger "$TARGET_USER"

  note "Wi-Fi power saving off on every saved Wi-Fi connection"
  nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-11-wireless"{print $1}' |
    while read -r c; do nmcli connection modify "$c" 802-11-wireless.powersave 2; done

  note "enable system services"
  systemctl daemon-reload
  udevadm control --reload-rules
  systemctl enable camilladsp.service camillagui.service wifi-recover.timer

  echo
  echo "Next: reboot, flash the XVF3800 firmware if it is new (docs/hardware.md),"
  echo "then run ./install.sh bootstrap-user as $TARGET_USER."
}

cmd_bootstrap_user() {
  [ "$(id -u)" -ne 0 ] || die "Run as the speaker's user, not root."
  local v req

  note "Python environments"
  for v in voice-agent wake-word xvf3800; do
    [ -x "$HOME/.venvs/$v/bin/python" ] || python3 -m venv "$HOME/.venvs/$v"
    "$HOME/.venvs/$v/bin/pip" install -q -r "$REPO/deps/requirements-$v.txt"
  done
  [ -x "$HOME/.local/share/volume-led/venv/bin/python" ] || python3 -m venv "$HOME/.local/share/volume-led/venv"
  "$HOME/.local/share/volume-led/venv/bin/pip" install -q -r "$REPO/deps/requirements-volume-led.txt"

  note "XVF3800 control library (pinned)"
  if [ ! -d "$HOME/src/reSpeaker_XVF3800_USB_4MIC_ARRAY/.git" ]; then
    git clone "$XVF_REPO" "$HOME/src/reSpeaker_XVF3800_USB_4MIC_ARRAY"
  fi
  git -C "$HOME/src/reSpeaker_XVF3800_USB_4MIC_ARRAY" checkout -q "$XVF_REPO_COMMIT"

  note "Vosk model ($VOSK_MODEL)"
  if [ ! -d "$HOME/.local/share/ai-speaker/$VOSK_MODEL" ]; then
    mkdir -p "$HOME/.local/share/ai-speaker"
    curl -fL -o "/tmp/$VOSK_MODEL.zip" "https://alphacephei.com/vosk/models/$VOSK_MODEL.zip"
    unzip -q "/tmp/$VOSK_MODEL.zip" -d "$HOME/.local/share/ai-speaker" && rm "/tmp/$VOSK_MODEL.zip"
  fi

  note "spotifyd $SPOTIFYD_VERSION (patched, built from source; takes a while)"
  if ! "$HOME/.local/bin/spotifyd" --version 2>/dev/null | grep -q "${SPOTIFYD_VERSION#v}"; then
    command -v cargo >/dev/null || [ -x "$HOME/.cargo/bin/cargo" ] || \
      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
    # shellcheck disable=SC1091
    . "$HOME/.cargo/env"
    local src=$HOME/.cache/spotifyd/src-$SPOTIFYD_VERSION
    [ -d "$src" ] || git clone --depth 1 --branch "$SPOTIFYD_VERSION" https://github.com/Spotifyd/spotifyd.git "$src"
    git -C "$src" apply --check "$REPO/deps/patches/spotifyd-0.4.2-no-mixer-volume.patch" 2>/dev/null \
      && git -C "$src" apply "$REPO/deps/patches/spotifyd-0.4.2-no-mixer-volume.patch"
    (cd "$src" && cargo build --release --locked --no-default-features --features pulseaudio_backend,dbus_mpris)
    install -Dm755 "$src/target/release/spotifyd" "$HOME/.local/bin/spotifyd"
  fi

  note "secrets (templates only; fill them in)"
  install -d -m 0700 "$HOME/.config/ai-speaker"
  for req in voice-agent.env spotify.json; do
    [ -e "$HOME/.config/ai-speaker/$req" ] || install -m 0600 "$REPO/config/$req.example" "$HOME/.config/ai-speaker/$req"
  done

  note "speaker files"
  cmd_apply user

  note "enable user services"
  systemctl --user daemon-reload
  systemctl --user enable xvf-aec-ref volume-led volume-sync volume-buttons welcome-sound \
    timer-alarm wake-word voice-agent spotifyd thermal-monitor

  echo
  echo "Still to do by hand (docs/operations.md, 'New device'):"
  echo "  - put the API key in ~/.config/ai-speaker/voice-agent.env"
  echo "  - restore private assets: tools/private-backup.sh restore <archive> (verifier model, Spotify login, keys)"
  echo "  - or authorise Spotify: ~/.venvs/voice-agent/bin/python ~/.local/bin/spotify_web.py auth"
  echo "  - reboot, then: ai-speaker-status.sh"
}

# --- main ----------------------------------------------------------------------------

case "${1:-}" in
  diff)             cmd_diff "${2:-all}" "${3:-}" ;;
  apply)            cmd_apply "${2:-all}" "${3:-}" ;;
  capture)          cmd_capture "${2:-all}" ;;
  bootstrap-system) cmd_bootstrap_system ;;
  bootstrap-user)   cmd_bootstrap_user ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
