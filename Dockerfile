FROM ubuntu
RUN apt-get update \
&&  apt-get install -y \
    build-essential \
    python-dev
WORKDIR /wd
COPY bin bin
COPY idlememstat idlememstat
COPY setup.py setup.py
RUN python setup.py install
ENTRYPOINT idlememstat
