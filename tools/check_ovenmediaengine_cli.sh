#!/bin/sh
set -eu

binary="${1:-/opt/ovenmediaengine/bin/OvenMediaEngine}"

check_info() {
    flag="$1"
    expected="$2"
    status=0
    output="$("$binary" "$flag" 2>&1)" || status=$?
    if [ "$status" -ne 1 ]; then
        printf 'Unexpected OME %s exit status: %s\n' "$flag" "$status" >&2
        printf '%s\n' "$output" >&2
        return 1
    fi
    if ! printf '%s\n' "$output" | grep -E "$expected" >/dev/null; then
        printf 'Unexpected OME %s output\n%s\n' "$flag" "$output" >&2
        return 1
    fi
    printf '%s\n' "$output"
}

# Pinned v0.21.0 intentionally returns PARENT_FAIL for both information flags.
check_info -h '^Usage:'
check_info -v '^OvenMediaEngine v?0\.21\.0([[:space:](]|$)'
