ARG BUILD_FROM
FROM $BUILD_FROM

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apk add --no-cache \
    python3 \
    py3-pip \
    ca-certificates \
    && update-ca-certificates \
    && pip3 install --no-cache-dir \
    flask==3.0.0 \
    pyyaml==6.0.1 \
    requests==2.31.0

COPY app.py /app.py
COPY run.sh /run.sh

RUN chmod a+x /run.sh

WORKDIR /

CMD [ "/run.sh" ]
