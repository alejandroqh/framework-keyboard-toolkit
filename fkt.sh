#!/usr/bin/env bash
# fkt: install, configure and inspect framework-keyboard-toolkit.
set -euo pipefail

NAME=framework-keyboard-toolkit
BIN=/usr/local/sbin/$NAME
UNIT=/etc/systemd/system/$NAME.service
RULE=/etc/udev/rules.d/90-$NAME.rules
RULE_NUMLOCK=/etc/udev/rules.d/90-$NAME-numlock.rules
SYSUSERS=/etc/sysusers.d/$NAME.conf
CONF=/etc/$NAME.conf
GROUP=fw-kbd-toolkit
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Usage: ./fkt.sh <command>

  install [SETTING=VALUE...]  install or update, and start the service
  configure                   ask what to switch on, then apply it
  set SETTING=VALUE...        change settings and apply them
  status                      show settings, modules, light and NumLock
  logs [-f]                   show the service log (-f keeps following)
  restart                     restart the service
  uninstall                   stop, disable and remove everything
  help                        show this

Settings:
  sync=yes|no      keyboard and numpad share one backlight level
  ambient=yes|no   the level follows the ambient light sensor
  numlock=yes|no   turn NumLock on at boot and login
  bands="8:85 60:170 250:255 *:0"
                   backlight level (0-255) below each lux limit, "*" = brighter

Examples:
  ./fkt.sh install
  ./fkt.sh set ambient=no
  ./fkt.sh set numlock=no bands="8:85 60:170 *:255"
USAGE
}

# --- settings ------------------------------------------------------------------

declare -A SET=()

valid_bands() {
    local -a items
    local item
    read -ra items <<< "$1"
    [[ ${#items[@]} -gt 0 && ${items[-1]} == \*:* ]] || return 1
    for item in "${items[@]}"; do
        [[ $item =~ ^([0-9]+(\.[0-9]+)?|\*):[0-9]{1,3}$ ]] || return 1
    done
}

# Check SETTING=VALUE arguments and collect them in SET.
parse_settings() {
    local arg key value
    for arg in "$@"; do
        [[ $arg == *=* ]] || die "'$arg' is not SETTING=VALUE (see ./fkt.sh help)"
        key=${arg%%=*}; value=${arg#*=}
        case $key in
            sync|ambient|numlock)
                [[ $value == yes || $value == no ]] || die "$key: use yes or no" ;;
            bands)
                valid_bands "$value" || die "bands: use \"LUX:LEVEL ... *:LEVEL\", for example \"8:85 60:170 *:255\"" ;;
            *) die "unknown setting '$key' (sync, ambient, numlock, bands)" ;;
        esac
        SET[$key]=$value
    done
}

# yes / no value of setting "$1" in $CONF (yes when it is not there).
conf_get() {
    local value
    value=$(sed -nE "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*([^#[:space:]]*).*/\1/p" "$CONF" | tail -n 1)
    case ${value,,} in
        no|false|off|0) echo no ;;
        *)              echo yes ;;
    esac
}

conf_set() {
    if grep -qE "^[[:space:]]*$1[[:space:]]*=" "$CONF"; then
        sed -i -E "s/^[[:space:]]*$1[[:space:]]*=.*/$1 = $2/" "$CONF"
    else
        printf '%s = %s\n' "$1" "$2" >> "$CONF"
    fi
}

# ask <setting> <question>: Enter keeps the current value.
ask() {
    local current answer
    current=$(conf_get "$1")
    while true; do
        read -r -p "$2 [$([[ $current == yes ]] && echo Y/n || echo y/N)] " answer
        case ${answer,,} in
            "")    return ;;
            y|yes) conf_set "$1" yes; return ;;
            n|no)  conf_set "$1" no; return ;;
        esac
    done
}

# Create / update $CONF from SET; "ask" also asks about the three switches.
configure() {
    local first=no key
    if [[ ! -e $CONF ]]; then
        first=yes
        install -m 0644 "$SRC_DIR/packaging/$NAME.conf" "$CONF"
    fi
    for key in "${!SET[@]}"; do
        conf_set "$key" "${SET[$key]}"
    done
    # Ask on request, and on a first install from a terminal with no choices given.
    if [[ ${1:-} == ask ]] || [[ $first == yes && ${#SET[@]} -eq 0 && -t 0 ]]; then
        ask sync    "Keep keyboard and numpad backlight at the same level?"
        ask ambient "Let the backlight follow the ambient light sensor?"
        ask numlock "Turn NumLock on at boot and login (needs access to /dev/uinput)?"
    fi
    chmod 0644 "$CONF"
    command -v restorecon >/dev/null && restorecon "$CONF" || true
    info "Configuration ($CONF): sync $(conf_get sync), ambient light $(conf_get ambient), NumLock $(conf_get numlock)"
}

# --- device access ---------------------------------------------------------------

# /dev nodes of the input modules' VIA raw-HID interface (usage page 0xFF60).
via_nodes() {
    local h
    for h in /sys/class/hidraw/hidraw*; do
        [[ -r $h/device/uevent && -r $h/device/report_descriptor ]] || continue
        grep -qE '^HID_ID=[0-9A-F]+:000032AC:0000(0012|0013|0014|0018|0019)$' "$h/device/uevent" || continue
        [[ $(od -An -tx1 -N3 "$h/device/report_descriptor" | tr -d ' \n') == 0660ff ]] || continue
        echo "/dev/${h##*/}"
    done
}

# True if a process with only group $GROUP can read and write "$1".
group_can_use() {
    setpriv --reuid=65534 --regid="$GROUP" --clear-groups -- test -r "$1" -a -w "$1"
}

# Make sure group $GROUP can really use node "$1" now, not only after a reboot.
# udev's MODE does not reach the group entry of a node that already carries a
# logind ACL (it only moves the ACL mask); nodes created later get it right.
grant() {
    group_can_use "$1" && return
    setfacl -m g::rw "$1" 2>/dev/null || true
    group_can_use "$1"
}

# Re-run the udev rules for the nodes the rule files are about.
retrigger() {
    local node
    udevadm control --reload
    while read -r node; do
        udevadm trigger --action=change "$node"
    done < <(via_nodes)
    if [[ -e /dev/uinput ]]; then
        udevadm trigger --action=change /dev/uinput
    fi
    udevadm settle
}

# Group, udev rules and node permissions for what $CONF switches on.
setup_access() {
    local node
    info "Granting group $GROUP access to the modules (udev rule)"
    install -D -m 0644 "$SRC_DIR/packaging/sysusers.conf" "$SYSUSERS"
    install -D -m 0644 "$SRC_DIR/packaging/90-$NAME.rules" "$RULE"
    if [[ $(conf_get numlock) == yes ]]; then
        info "Granting group $GROUP access to /dev/uinput (for NumLock)"
        install -D -m 0644 "$SRC_DIR/packaging/90-$NAME-numlock.rules" "$RULE_NUMLOCK"
        [[ -e /dev/uinput ]] || modprobe uinput 2>/dev/null || true
    else
        rm -f "$RULE_NUMLOCK"
    fi
    command -v restorecon >/dev/null && restorecon "$SYSUSERS" "$RULE" "$RULE_NUMLOCK" 2>/dev/null || true
    systemd-sysusers "$SYSUSERS"
    retrigger
    while read -r node; do
        [[ $(stat -c %G "$node") == "$GROUP" ]] \
            || die "udev rule did not apply to $node (expected group $GROUP)."
        grant "$node" \
            || warn "group $GROUP cannot open $node yet; it will after a reboot or re-seating the module."
    done < <(via_nodes)
    if [[ $(conf_get numlock) == yes ]]; then
        if [[ ! -e /dev/uinput ]]; then
            warn "/dev/uinput does not exist: NumLock will not be turned on."
        elif ! grant /dev/uinput; then
            warn "group $GROUP cannot open /dev/uinput yet: NumLock will be turned on after a reboot."
        fi
    fi
}

# --- service -----------------------------------------------------------------------

start_service() {
    systemctl daemon-reload
    systemctl enable "$NAME.service"
    systemctl restart "$NAME.service"
    sleep 3
    if systemctl is-active --quiet "$NAME.service"; then
        # What this run of the service found (modules, light, NumLock).
        journalctl -o cat --no-pager \
            "_SYSTEMD_INVOCATION_ID=$(systemctl show -P InvocationID "$NAME.service")" || true
        info "Done. See what it does with:  ./fkt.sh logs -f"
    else
        systemctl status "$NAME.service" --no-pager || true
        die "Service failed to start."
    fi
}

need_installed() {
    [[ -e $UNIT && -e $CONF ]] || die "$NAME is not installed yet: run ./fkt.sh install"
}

# --- commands ----------------------------------------------------------------------

cmd_install() {
    [[ -n $(via_nodes) ]] || die "No Framework Laptop 16 keyboard, numpad or macropad module found."
    if ! command -v python3 >/dev/null; then
        if command -v dnf >/dev/null; then
            info "Installing dependency: python3"
            dnf install -y python3
        elif command -v pacman >/dev/null; then
            info "Installing dependency: python"
            pacman -S --needed --noconfirm python
        else
            die "python3 is missing; install it and run this again."
        fi
    fi
    configure
    setup_access
    info "Installing $BIN and $UNIT"
    install -m 0755 "$SRC_DIR/$NAME" "$BIN"
    install -m 0644 "$SRC_DIR/$NAME.service" "$UNIT"
    command -v restorecon >/dev/null && restorecon "$BIN" "$UNIT" || true
    start_service
}

# configure / set: new settings for an installed service.
cmd_apply() {
    need_installed
    configure "$@"
    setup_access
    start_service
}

cmd_uninstall() {
    info "Stopping and disabling $NAME"
    systemctl disable --now "$NAME.service" 2>/dev/null || true
    rm -f "$BIN" "$UNIT" "$RULE" "$RULE_NUMLOCK" "$SYSUSERS" "$CONF"
    systemctl daemon-reload
    retrigger || true
    groupdel "$GROUP" 2>/dev/null || true
    info "Removed. Backlight levels and NumLock stay as they are now."
}

cmd_status() {
    local tool=$BIN
    if [[ -e $UNIT ]]; then
        echo "service:   $(systemctl is-active "$NAME.service" || true), $(systemctl is-enabled "$NAME.service" 2>/dev/null || true) at boot"
    else
        echo "service:   not installed"
        tool=$SRC_DIR/$NAME
    fi
    "$tool" --status
}

cmd_logs() {
    if [[ ${1:-} == -f ]]; then
        exec journalctl -u "$NAME.service" -n 20 -f
    fi
    journalctl -u "$NAME.service" -b -n 40 --no-pager
}

# Run this script again as root, for the commands that need it.
as_root() {
    if [[ $EUID -ne 0 ]]; then
        exec sudo -- "$0" "$@"
    fi
}

command=${1:-help}
shift || true
case $command in
    install)
        parse_settings "$@"
        as_root install "$@"
        cmd_install ;;
    configure)
        [[ $# -eq 0 ]] || die "configure takes no arguments; to set values directly use ./fkt.sh set"
        [[ -t 0 ]] || die "configure asks questions and needs a terminal; use ./fkt.sh set SETTING=VALUE instead."
        as_root configure
        cmd_apply ask ;;
    set)
        [[ $# -gt 0 ]] || die "set what? For example: ./fkt.sh set ambient=no"
        parse_settings "$@"
        as_root set "$@"
        cmd_apply ;;
    restart)
        need_installed
        as_root restart
        systemctl restart "$NAME.service"
        info "Restarted." ;;
    uninstall)
        as_root uninstall
        cmd_uninstall ;;
    status)  cmd_status ;;
    logs)    cmd_logs "$@" ;;
    help|-h|--help) usage ;;
    *) die "unknown command '$command' (see ./fkt.sh help)" ;;
esac
