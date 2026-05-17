ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.19
FROM ${BUILD_FROM}

RUN apk add --no-cache \
    python3 \
    py3-pip \
    bash \
    curl

WORKDIR /app

COPY requirements.txt .

RUN pip3 install \
    --break-system-packages \
    --no-cache-dir \
    -r requirements.txt

COPY rootfs /

RUN chmod a+x /etc/services.d/eufy-c30-mqtt/run

CMD [ "/init" ]
