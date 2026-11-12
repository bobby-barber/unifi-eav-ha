#!/bin/bash
# CEC forwarding for a UniFi EAV-Bridge, run over SSH by the Home Assistant integration.
#
# Usage (the script is piped to the bridge, nothing is stored on it):
#   bash -s -- status
#   bash -s -- host   <channel>     source bridge; channel is the console's route channel (e.g. 0002)
#   bash -s -- client <source ip>   receiver bridge; links to the source bridge at that address
#   bash -s -- off                  stop forwarding started by this script
#
# Why this exists (full story in docs/cec.md):
#   EAV-Bridges are built on ASPEED's video-over-IP SDK, which can carry HDMI-CEC between a
#   source and its receivers. UniFi's firmware switches that off: /etc/init.d/rcS sets
#   NO_CEC=y on every boot, and the bridge's root filesystem is a RAM disk reloaded from
#   flash, so nothing changed on the bridge survives a restart. This script starts the SDK's
#   own CEC service by hand, from a temporary copy that has CEC switched on, and links it.
#   A restart of the bridge undoes all of it; the integration then runs this script again.
#
# Safe to run repeatedly: when forwarding is already up with the wanted target it changes
# nothing. It never writes to flash. The last line of output is always one machine-readable
# summary:
#   HA_CEC result=<ok|started|stopped|off|error> mode=... state=... target=... detail=...

export PATH=/bin:/sbin:/usr/bin:/usr/sbin:/usr/local/bin
MODE=${1:-status}
ARG=${2:-}
SDK=/usr/local/bin
CEC_SYS=/sys/devices/platform/cec
LM=/tmp/ha_cec_lm.sh          # our patched copy of the SDK's CEC link manager
LM_LOG=/var/log/ha_cec_lm.log
STATE=
TARGET=
NOCEC=

say() { # result detail
    echo "HA_CEC result=$1 mode=$MODE state=${STATE:-none} target=${TARGET:-none} detail=$2"
    exit 0
}

cd "$SDK" 2>/dev/null || say error no_sdk_dir

bridge_role() {
    case " $(cat /proc/cmdline) " in
        *" uav_mode=host "*) echo host ;;
        *) echo client ;;
    esac
}

# Ask the running CEC link manager for its variables. Fails if none is running.
read_state() {
    STATE= TARGET= NOCEC=
    local dump
    dump=$(timeout 5 ipc @c_lm_query q ce_param_dump 2>/dev/null) || return 1
    STATE=$(echo "$dump" | sed -n 's/^C_STATE=//p' | head -n 1)
    TARGET=$(echo "$dump" | sed -n 's/^C_CH_SELECT=//p' | head -n 1)
    NOCEC=$(echo "$dump" | sed -n 's/^NO_CEC=//p' | head -n 1)
    [ -n "$STATE" ]
}

ours_running() { ps w | grep -q '[h]a_cec_lm\.sh'; }

# Stop every copy of the CEC link manager, its IPC server and the cec app. The link manager
# traps SIGTERM, so SIGKILL is the only reliable stop. The kernel driver stays loaded.
stop_all() {
    local pids
    # (lm[hc]_cec.sh: hand-started copies from the first manual test, 2026-11-11)
    pids=$(ps w | awk '/[l]ink_mgr[hc]_cec|[h]a_cec_lm|[l]m[hc]_cec\.sh|[i]pc_server @c_lm_set/ {print $1}
                       $5 == "cec" {print $1}')
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null
    sleep 1
    rm -f /var/lock/@c_lm_query.lck /var/lock/@c_ctrl.lck
}

# The source's route channel as the bridge itself uses it: its video multicast group is
# 225.2.<first two digits>.<last two digits> on UDP 59002 (SDK ip_mapping.sh).
local_channel() {
    netstat -anu 2>/dev/null | awk '$4 ~ /^225\.2\.[0-9]+\.[0-9]+:59002$/ {
        split($4, a, "[.:]"); printf "%02d%02d\n", a[3], a[4]; exit }'
}

wait_for_manager() {
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        read_state && return 0
        sleep 1
    done
    return 1
}

start_manager() { # sdk script name
    stop_all
    # The SDK script reads NO_CEC=y from /var/lm_var; switch it back on right after that.
    sed 's#^\t\tsource /var/lm_var#\t\tsource /var/lm_var; NO_CEC=n#' "$1" > "$LM" \
        || say error copy_failed
    grep -q 'source /var/lm_var; NO_CEC=n' "$LM" || say error patch_failed
    # Detach so it outlives this SSH session (SIGHUP ignored).
    ( trap '' HUP; exec bash "$LM" > "$LM_LOG" 2>&1 < /dev/null ) &
    wait_for_manager || say error manager_did_not_start
    [ -w "$CEC_SYS/forward_rule" ] || say error cec_driver_missing
    echo 0xff > "$CEC_SYS/forward_rule"   # pass every CEC message (UniFi's own choice too)
}

case "$MODE" in
status)
    if ours_running && read_state; then
        say running "nocec=$NOCEC"
    fi
    say off not_running
    ;;
off)
    stop_all
    [ -w "$CEC_SYS/forward_rule" ] && echo 0x00 > "$CEC_SYS/forward_rule"
    say stopped done
    ;;
host)
    [ "$(bridge_role)" = host ] || say error not_a_source
    ch=$(local_channel)
    note=channel_from_bridge
    if [ -z "$ch" ]; then
        ch=$ARG
        note=channel_from_console
    fi
    case "$ch" in
        [0-9][0-9][0-9][0-9]) ;;
        *) say error "no_channel(${ARG:-none})" ;;
    esac
    [ -n "$ARG" ] && [ "$ARG" != "$ch" ] && note="${note},console_said_$ARG"
    if ours_running && read_state && [ "$NOCEC" = n ] && [ "$TARGET" = "$ch" ]; then
        case "$STATE" in s_pre_start_srv|s_srv_on) say ok "$note" ;; esac
    fi
    start_manager link_mgrh_cec.sh
    # Tell receivers that ask that this source has CEC on.
    node_query --if essential --of essential --set_key NO_CEC=n > /dev/null 2>&1
    timeout 5 ipc @c_lm_set s "ce_start:$ch"
    for i in 1 2 3 4 5; do
        sleep 1
        read_state
        case "$STATE" in s_pre_start_srv|s_srv_on) say started "$note" ;; esac
    done
    say error "source_not_ready,$note"
    ;;
client)
    [ "$(bridge_role)" = client ] || say error not_a_receiver
    case "$ARG" in
        ?*.?*.?*.?*) ;;
        *) say error no_source_ip ;;
    esac
    if ours_running && read_state && [ "$NOCEC" = n ] && [ "$TARGET" = "$ARG" ] \
        && [ "$STATE" = s_srv_on ]; then
        say ok linked
    fi
    start_manager link_mgrc_cec.sh
    # By address: UniFi doesn't run the SDK's name lookup, so a channel name finds nothing.
    for attempt in 1 2; do
        timeout 5 ipc @c_lm_set s ce_stop > /dev/null 2>&1
        timeout 5 ipc @c_lm_set s "ce_start:$ARG"
        for i in 1 2 3 4 5 6 7 8 9 10; do
            sleep 1
            read_state
            [ "$STATE" = s_srv_on ] && say started linked
        done
    done
    say error source_did_not_answer
    ;;
*)
    say error "unknown_mode"
    ;;
esac
