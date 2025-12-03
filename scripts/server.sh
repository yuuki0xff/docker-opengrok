#!/bin/bash
set -eu

# Setup SSH files from secrets.
if [ -f /run/secrets/ssh_private_key ]; then
    # NOTE: Destination file name that is included in the default values of ssh's IdentityFile.
    # It does not matter if the actual key pair type is different.
    install -d -m 700 /root/.ssh
    install -m 400 /run/secrets/ssh_private_key /root/.ssh/id_ed25519
    install -m 400 /run/secrets/ssh_public_key /root/.ssh/id_ed25519.pub
    install -m 600 /run/secrets/ssh_known_hosts /root/.ssh/known_hosts
fi

if [ ! -f /opengrok/etc/configuration.xml ]; then
    echo "No configuration file found, initializing..."
    opengrok-indexer \
        -J=-Djava.util.logging.config.file=/opengrok/etc/logging.properties \
        -a /opengrok/lib/opengrok.jar -- \
        -c /usr/local/bin/ctags \
        -s /opengrok/src \
        -d /opengrok/data \
        -H -P -G \
        --renamedHistory on \
        --webappCtags on \
        -W /opengrok/etc/configuration.xml
    echo "Configuration file initialized"
fi

echo "Starting Tomcat..."
/usr/local/tomcat/bin/catalina.sh run &

wait
