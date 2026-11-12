"""Agentic Bot settings, with dynamic legacy environment fallback."""
import os

def env_get(name, default=None):
    if name in os.environ:
        return os.environ[name]
    if name.startswith('AGENTIC_'):
        return os.environ.get('GROK_' + name[len('AGENTIC_'):], default)
    if name.startswith('AGENTICBOT_'):
        return os.environ.get('GROKBOT_' + name[len('AGENTICBOT_'):], default)
    return os.environ.get(name, default)
