"""POSIX command bootstrap. All setup runs in a fresh interpreter after exec."""
import os
import sys
import resource


def main():
    cpu, size, pids = map(int, sys.argv[1:4])
    for kind, limits in ((resource.RLIMIT_CPU, (cpu, cpu + 5)),
                         (resource.RLIMIT_FSIZE, (size, size))):
        try:
            resource.setrlimit(kind, limits)
        except (ValueError, OSError):
            pass
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        limit = pids if hard == resource.RLIM_INFINITY else min(pids, hard)
        resource.setrlimit(resource.RLIMIT_NPROC, (limit, hard))
    except (ValueError, OSError):
        pass
    os.execv(sys.argv[4], sys.argv[4:])


if __name__ == '__main__':
    main()
