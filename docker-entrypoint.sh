#!/bin/sh
# We run as an arbitrary PUID:PGID (see docker-compose.yml), which normally has no /etc/passwd entry.
# OpenSSH (and therefore Ansible) refuses to run in that case ("No user exists for uid ..."),
# so synthesize an entry with libnss-wrapper instead of baking one uid into the image.
if ! getent passwd "$(id -u)" >/dev/null 2>&1; then
    NSS_LIB=$(find /usr/lib -name libnss_wrapper.so 2>/dev/null | head -n 1)
    if [ -n "$NSS_LIB" ]; then
        echo "deployer:x:$(id -u):$(id -g):deployer:${HOME:-/tmp}:/bin/sh" > /tmp/nss_passwd
        { cat /etc/group; echo "deployer:x:$(id -g):"; } > /tmp/nss_group
        export NSS_WRAPPER_PASSWD=/tmp/nss_passwd NSS_WRAPPER_GROUP=/tmp/nss_group LD_PRELOAD="$NSS_LIB"
    fi
fi
exec "$@"
