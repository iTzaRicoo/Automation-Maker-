ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install Python
RUN apk add --no-cache python3 py3-pip

# Install Python requirements
COPY requirements.txt /requirements.txt
RUN pip3 install --no-cache-dir -r /requirements.txt

# Copy app files
COPY app.py /app.py
COPY index.html /index.html
COPY run.sh /run.sh

RUN chmod a+x /run.sh

WORKDIR /
ENTRYPOINT [ "/run.sh" ]
