FROM mcr.microsoft.com/devcontainers/python:1-3.13-bookworm

RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        fonts-liberation \
        fonts-noto-cjk \
        fonts-crosextra-carlito \
        libreoffice-calc \
        libreoffice-impress \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

    COPY .devcontainer/fonts.conf /etc/fonts/local.conf
    COPY .devcontainer/fonts/ /usr/local/share/fonts/custom/

    RUN fc-cache -f

COPY poc_image_convert/requirements.txt /tmp/poc-requirements.txt

RUN python -m pip install --no-cache-dir -r /tmp/poc-requirements.txt