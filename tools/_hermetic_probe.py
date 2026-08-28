import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.argv = ['simulate.py', '--list']
import importlib.util
spec = importlib.util.spec_from_file_location('sim', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'simulate.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
import requests
try:
    requests.get('https://example.com')
    print('NOT_BLOCKED')
except AssertionError:
    print('BLOCKED')
