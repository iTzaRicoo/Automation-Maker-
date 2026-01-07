ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache \
    python3 \
    py3-pip \
    && pip3 install --no-cache-dir \
    flask==3.0.0 \
    flask-cors==4.0.0 \
    pyyaml==6.0.1 \
    requests==2.31.0

COPY automation_maker/main.py /app.py
COPY automation_maker/index.html /index.html
COPY automation_maker/styles.css /styles.css
COPY automation_maker/script.js /script.js
COPY run.sh /run.sh

RUN chmod a+x /run.sh

WORKDIR /

# ❌ VERWIJDER deze regel:
# ENTRYPOINT ["/init"]

# ✅ Alleen CMD gebruiken:
CMD [ "/run.sh" ]
