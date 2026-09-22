import os
import socket
import pytest


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    # These changes apply only to each experiment process/test, never the user's environment.
    for name in tuple(os.environ):
        if name.startswith(('LANGSMITH_', 'LANGCHAIN_')):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('LANGSMITH_TRACING', 'false')
    monkeypatch.setenv('LANGCHAIN_TRACING_V2', 'false')
    attempts = []
    def deny(*args, **kwargs):
        attempts.append('blocked')
        raise AssertionError('The M07 experiment must not use network services.')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)
    yield
    assert not attempts, 'A library attempted networking even though tracing was disabled.'
