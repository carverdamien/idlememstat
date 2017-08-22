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
ENTRYPOINT [ "idlememstat" ]
ENV KPAGEFLAGS_PATH /proc/kpageflags
ENV KPAGECGROUP_PATH /proc/kpagecgroup
ENV KPAGEIDLE_PATH /sys/kernel/mm/page_idle/bitmap
ENV MEMCG_ROOT_PATH /sys/fs/cgroup/memory
ENV ZONE_INFO_PATH /proc/zoneinfo
CMD [ "1" ]
