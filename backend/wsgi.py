"""WSGI entry point for the persistent API host."""
import atexit
from backend.server import app, server
server.setup()
atexit.register(server.close)
