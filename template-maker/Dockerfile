ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache \
    python3 \
    py3-pip \
    && pip3 install --no-cache-dir \
    flask==3.0.0 \
    pyyaml==6.0.1 \
    requests==2.31.0

COPY app.py /app.py
COPY run.sh /run.sh

RUN chmod a+x /run.sh

WORKDIR /

CMD [ "/run.sh" ]
