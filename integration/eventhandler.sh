#!/bin/sh
# SPDX-License-Identifier: MIT
# Accept a saved calendar attachment or a complete piped NeoMutt mail.
rsvp_python=${NEOMUTT_RSVP_PYTHON:-python3}
case "$rsvp_python" in
    /*) PATH="$(dirname "$rsvp_python"):$PATH"; export PATH ;;
esac
exec "$rsvp_python" "$(dirname "$(readlink -f "$0")")/calendar-reply.py" "$@"
