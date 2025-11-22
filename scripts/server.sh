#!/bin/bash
set -eu

if [ ! -f /opengrok/etc/configuration.xml ]; then
    echo "No configuration file found, initializing..."
    mkdir -p /tmp/empty-source-root /tmp/empty-data-root
    opengrok-indexer \
        -J=-Djava.util.logging.config.file=/opengrok/etc/logging.properties \
        -a /opengrok/lib/opengrok.jar -- \
        -c /usr/local/bin/ctags \
        -s /tmp/empty-source-root \
        -d /tmp/empty-data-root \
        -H -P -G \
        --renamedHistory on \
        --webappCtags on \
        -W /opengrok/etc/configuration.xml
    sed -i 's@/tmp/empty-source-root@/opengrok/src@g' /opengrok/etc/configuration.xml
    sed -i 's@/tmp/empty-data-root@/opengrok/data@g' /opengrok/etc/configuration.xml
    echo "Configuration file initialized"
fi

echo "Starting Tomcat..."
/usr/local/tomcat/bin/catalina.sh run &

wait
