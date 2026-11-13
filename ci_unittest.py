"""Run live unittest suites with a cross-platform per-test wall-clock deadline.

A watchdog exits the process (including wedged non-daemon browser workers) after
printing all thread stacks. CI's step/job deadlines remain an outer safety net.
"""
import argparse
import faulthandler
import os
import threading
import sys
import unittest


class DeadlineResult(unittest.TextTestResult):
    timeout = 90.0

    def startTest(self, test):
        super().startTest(test)
        self._watchdog = threading.Timer(self.timeout, self._expired, args=(test,))
        self._watchdog.daemon = True
        self._watchdog.start()

    def _expired(self, test):
        self.stream.write('\nTIMEOUT after %ss: %s\n' % (self.timeout, test))
        self.stream.flush()
        faulthandler.dump_traceback(all_threads=True)
        os._exit(124)

    def stopTest(self, test):
        self._watchdog.cancel()
        super().stopTest(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('modules', nargs='+')
    parser.add_argument('-v', action='store_true')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    DeadlineResult.timeout = args.timeout
    sys.path.insert(0, os.getcwd())
    suite = unittest.defaultTestLoader.loadTestsFromNames(args.modules)
    result = unittest.TextTestRunner(resultclass=DeadlineResult,
                                     verbosity=2 if args.v else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
