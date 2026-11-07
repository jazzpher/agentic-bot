"""Deprecated import alias. Use agenticbot instead."""
import importlib
import importlib.abc
import importlib.util
import sys
import warnings
from agenticbot import __version__
warnings.warn('grokbot is deprecated; use agenticbot', DeprecationWarning, stacklevel=2)

class _Alias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith('grokbot.') or fullname == 'grokbot.__main__':
            return None
        new = 'agenticbot' + fullname[len('grokbot'):]
        spec = importlib.util.find_spec(new)
        if spec is None:
            return None
        return importlib.util.spec_from_loader(fullname, self, is_package=spec.submodule_search_locations is not None)
    def create_module(self, spec):
        return importlib.import_module('agenticbot' + spec.name[len('grokbot'):])
    def exec_module(self, module):
        pass

if not any(type(x).__module__ == __name__ for x in sys.meta_path):
    sys.meta_path.insert(0, _Alias())
