import optparse
import os
import os.path
import signal
import stat
import sys
import threading
import time
import docker
import pymongo

import kpageutil

MEMCG_ROOT_PATH = "/sys/fs/cgroup/memory"
if 'MEMCG_ROOT_PATH' in os.environ:
    MEMCG_ROOT_PATH = os.environ['MEMCG_ROOT_PATH']

ZONE_INFO_PATH = "/proc/zoneinfo"
if 'ZONE_INFO_PATH' in os.environ:
    ZONE_INFO_PATH = os.environ['ZONE_INFO_PATH']

DEFAULT_DELAY = 300  # seconds


def _get_end_pfn():
    end_pfn = 0
    with open(ZONE_INFO_PATH, 'r') as f:
        for l in f.readlines():
            l = l.split()
            if l[0] == 'spanned':
                end_pfn = int(l[1])
            elif l[0] == 'start_pfn:':
                end_pfn += int(l[1])
    return end_pfn

END_PFN = _get_end_pfn()
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


class IdleMemTracker:

    SCAN_CHUNK = 32768

    ##
    # interval: interval between updates, in seconds
    # on_update: callback to run on each update
    #
    # To avoid CPU bursts, the estimator will distribute the scanning in time
    # so that a full scan fits in the given interval.

    def __init__(self, interval, on_update=None):
        self.interval = interval
        self.on_update = on_update
        self.__is_shut_down = threading.Event()
        self.__should_shut_down = threading.Event()

    @staticmethod
    def __time():
        return time.time()

    # like sleep, but is interrupted by shutdown
    def __sleep(self, seconds):
        self.__should_shut_down.wait(seconds)

    def __init_scan(self):
        self.__nr_idle = {}
        self.__scan_pfn = 0
        self.__scan_time = 0.0
        self.__scan_start = self.__time()

    def __scan_done(self):
        if self.on_update:
            self.on_update(self)

    def __scan_iter(self):
        start_time = self.__time()
        start_pfn = self.__scan_pfn
        end_pfn = min(self.__scan_pfn + self.SCAN_CHUNK, END_PFN)
        # count idle pages
        cur = kpageutil.count_idle_pages_per_cgroup(start_pfn, end_pfn)
        # accumulate the result
        Z = (0, 0)
        tot = self.__nr_idle
        for k in set(cur.keys() + tot.keys()):
            tot[k] = map(sum, zip(tot.get(k, Z), cur.get(k, Z)))
        # mark the scanned pages as idle for the next iteration
        kpageutil.set_idle_pages(start_pfn, end_pfn)
        # advance the pos and accumulate the time spent
        self.__scan_pfn = end_pfn
        self.__scan_time += self.__time() - start_time

    def __throttle(self):
        pages_left = END_PFN - self.__scan_pfn
        time_left = self.interval - (self.__time() - self.__scan_start)
        time_required = pages_left * self.__scan_time / self.__scan_pfn
        if time_required > time_left:
            return
        chunks_left = float(pages_left) / self.SCAN_CHUNK
        self.__sleep((time_left - time_required) / chunks_left
                     if pages_left > 0 else time_left)

    def __scan(self):
        self.__scan_iter()
        self.__throttle()
        if self.__scan_pfn >= END_PFN:
            self.__scan_done()
            self.__init_scan()

    ##
    # Get the current idle memory estimate for the given cgroup ino.
    # Returns (anon_idle_bytes, file_idle_bytes) tuple.

    def get_idle_size(self, ino):
        nr_idle = self.__nr_idle.get(ino, (0, 0))
        return (nr_idle[0] * PAGE_SIZE, nr_idle[1] * PAGE_SIZE)

    ##
    # Scan memory periodically counting unused pages until shutdown.

    def serve_forever(self):
        self.__is_shut_down.clear()
        try:
            self.__init_scan()
            while not self.__should_shut_down.is_set():
                self.__scan()
        finally:
            self.__should_shut_down.clear()
            self.__is_shut_down.set()

    ##
    # Stop the serve_forever loop and wait until it exits.

    def shutdown(self):
        self.__should_shut_down.set()
        self.__is_shut_down.wait()


def get_memcg_usage(path):
    anon_usage, file_usage = 0, 0
    with open(os.path.join(path, 'memory.stat'), 'r') as f:
        for l in f.readlines():
            (k, v) = l.split()
            if k in ('active_anon', 'inactive_anon'):
                anon_usage += int(v)
            elif k in ('active_file', 'inactive_file'):
                file_usage += int(v)
    return (anon_usage, file_usage)


def print_idlemem_info_hdr():
    print "%-20s%10s%10s%10s%10s%10s%10s" % \
        ('cgroup', 'total', 'idle', "anon", "anon_idle", "file", "file_idle")

def idlemem_info(idlemem_tracker):
    for dir, subdirs, files in os.walk(MEMCG_ROOT_PATH):
        ino = os.stat(dir)[stat.ST_INO]
        idle = idlemem_tracker.get_idle_size(ino)
        total = get_memcg_usage(dir)
        cgroup = dir.replace(MEMCG_ROOT_PATH, '', 1) or '/'
        yield (cgroup, total, idle)

def print_idlemem_info(idlemem_tracker):
    for cgroup, total, idle in idlemem_info(idlemem_tracker):
        print "%-20s%10d%10d%10d%10d%10d%10d" % \
            (cgroup,
             (total[0] + total[1]) / 1024,
             (idle[0] + idle[1]) / 1024,
             total[0] / 1024, idle[0] / 1024,
             total[1] / 1024, idle[1] / 1024)

def print_docker_idlemem_info_hdr():
    print "%-20s%10s%10s%10s%10s%10s%10s" % \
        ('container', 'total', 'idle', "anon", "anon_idle", "file", "file_idle")

def print_docker_idlemem_info(idlemem_tracker):
    containers = [c['Id'] for c in docker.client.Client().containers()]
    for cgroup, total, idle in idlemem_info(idlemem_tracker):
        cid = cgroup.split('/')[-1]
        if cid not in containers: continue
        print "%-20s%10d%10d%10d%10d%10d%10d" % \
            (cid,
             (total[0] + total[1]) / 1024,
             (idle[0] + idle[1]) / 1024,
             total[0] / 1024, idle[0] / 1024,
             total[1] / 1024, idle[1] / 1024)

def idlemem_info_records(idlemem_tracker):
    def to_record(read, cgroup, to_record, idle):
        return {
            'read' : read,
            'cgroup' : cgroup,
            'Id' : cgroup.split('/')[-1],
            'memory_stats' : {
                'total' : total[0] + total[1],
                'stats' : {
                    'idle_anon' : idle[0],
                    'idle_file' : idle[1],
                    'anon' : total[0],
                    'file' : total[1],
                }
            }
        }
    read = time.strftime("%Y-%m-%dT%H:%M:%S") # FIX ME: should come from kpageutil.ccp
    return [to_record(read, cgroup, total, idle)
            for cgroup, total, idle in idlemem_info(idlemem_tracker)]

def _sighandler(signum, frame):
    global _shutdown_request
    _shutdown_request = True

def main():
    parser = optparse.OptionParser()
    parser.add_option("-d", dest="delay", default=DEFAULT_DELAY, type=int)
    parser.add_option("--cgroup", dest="cgroup", default=False, action="store_true")
    parser.add_option("--docker", dest="docker", default=False, action="store_true")
    parser.add_option("--mongo", dest="mongo", default=False, action="store_true")
    parser.add_option('--dbname', dest="dbname", type=str, nargs='?', default='prod')
    (options, args) = parser.parse_args()

    global _shutdown_request
    _shutdown_request = False
    signal.signal(signal.SIGINT, _sighandler)
    signal.signal(signal.SIGTERM, _sighandler)

    on_update_callbacks = []

    def on_update(idlemem_tracker):
        for c in on_update_callbacks:
            c(idlemem_tracker)

    if options.docker:
        print_docker_idlemem_info_hdr()
        on_update_callbacks.append(print_docker_idlemem_info)

    if options.cgroup:
        print_idlemem_info_hdr()
        on_update_callbacks.append(print_idlemem_info)

    if options.mongo:
        def mongo_idlemem_info(idlemem_tracker):
            with pymongo.MongoClient() as client:
                db = client[options.dbname]
                db.dockerstats.insert_many(idlemem_info_records(idlemem_tracker))
        on_update_callbacks.append(mongo_idlemem_info)

    idlemem_tracker = IdleMemTracker(options.delay, on_update)
    t = threading.Thread(target=idlemem_tracker.serve_forever)
    t.start()

    while not _shutdown_request and t.isAlive():
        time.sleep(1)

    if not t.isAlive():
        sys.stderr.write("Oops, tracker thread crashed unexpectedly!\n")
        sys.exit(1)

    idlemem_tracker.shutdown()
    sys.exit(0)

if __name__ == "__main__":
    main()
