#!/bin/bash
set -eu

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
