FROM opengrok/docker:1.14

CMD ["/bin/sleep", "infinity"]

RUN mkdir -p /usr/local/tomcat/webapps/ROOT && \
    cd /usr/local/tomcat/webapps/ROOT && \
    unzip /opengrok/lib/source.war && \
    sed -i 's@/var/opengrok/etc@/opengrok/etc@g' WEB-INF/web.xml

RUN apt update && apt install -y python3-pip pipx
ENV PATH="$PATH:/root/.local/bin"
COPY opengrok-manager/ /opengrok-manager/
RUN pipx install -e /opengrok-manager/
COPY scripts/ /scripts/
COPY example/ /example/
