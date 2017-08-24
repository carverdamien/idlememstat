FROM ubuntu
RUN apt-get update \
&&  apt-get install -y \
    build-essential \
    python-dev \
    python-pip
WORKDIR /wd
COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt
COPY bin bin
COPY idlememstat idlememstat
COPY setup.py setup.py
RUN python setup.py install
ENTRYPOINT [ "idlememstat" ]
ENV KPAGEFLAGS_PATH=/proc/kpageflags \
    KPAGECGROUP_PATH=/proc/kpagecgroup \
    KPAGEIDLE_PATH=/sys/kernel/mm/page_idle/bitmap \
    MEMCG_ROOT_PATH=/sys/fs/cgroup/memory \
    ZONE_INFO_PATH=/proc/zoneinfo
CMD [ "-d", "1" ]
