"""Runs in a fresh child process. Input is synthetic and delivered over stdin."""
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys

sys.dont_write_bytecode = True


def main():
    config = json.load(sys.stdin)
    for path in config['import_paths']:
        sys.path.insert(0, path)
    for name in tuple(os.environ):
        if name.startswith(('LANGSMITH_', 'LANGCHAIN_')):
            del os.environ[name]
    os.environ['LANGSMITH_TRACING'] = 'false'
    os.environ['LANGCHAIN_TRACING_V2'] = 'false'
    attempts = []
    def deny(*args, **kwargs):
        attempts.append('blocked')
        raise AssertionError('Network is disabled in this probe.')
    def stop_after_commit():
        assert not attempts, 'An imported library attempted networking.'
        os._exit(23)
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.create_connection = deny
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import Command, interrupt
    from typing import TypedDict
    from candidate import build_graph
    from fixtures import make_runner
    from juli.core.common import DomainError

    mode = config['mode']
    if mode in {'gap-crash', 'gap-resume', 'native-crash', 'native-read'}:
        runner = make_runner(config['task_config'])
        if mode == 'native-crash':
            runner.step(expected_actions=0)
            stop_after_commit()
        if mode == 'native-read':
            assert not attempts
            print(json.dumps({'outcome':runner.view()}))
            return
        with sqlite3.connect(config['checkpoint'], check_same_thread=False) as connection:
            saver = SqliteSaver(connection)
            hook = stop_after_commit if mode == 'gap-crash' else None
            graph = build_graph(runner, checkpointer=saver, after_commit=hook)
            graph_config = {'configurable':{'thread_id':'synthetic-gap'}}
            try:
                graph.invoke({'expected_actions':0, 'status':''} if mode=='gap-crash' else None,
                             graph_config, durability='sync')
                print(json.dumps({'error':None, 'outcome':runner.view()}))
            except DomainError as exc:
                print(json.dumps({'error':exc.code, 'outcome':runner.view(), 'graph_state':graph.get_state(graph_config).values}))
            assert not attempts
        return

    # Native LangGraph checkpoint/interrupt capability probe; separate from Juli's database.
    class QuestionState(TypedDict):
        answer: str
    def ask(state):
        # Deliberately non-idempotent synthetic side effect, to observe replay semantics.
        with sqlite3.connect(config['effects']) as effects:
            effects.execute('INSERT INTO visits DEFAULT VALUES')
        answer = interrupt('Synthetic clarification?')
        return {'answer':answer}
    with sqlite3.connect(config['checkpoint'], check_same_thread=False) as connection:
        builder = StateGraph(QuestionState)
        builder.add_node('ask',ask)
        builder.add_edge(START,'ask')
        builder.add_edge('ask',END)
        graph = builder.compile(checkpointer=SqliteSaver(connection))
        graph_config = {'configurable':{'thread_id':'synthetic-interrupt'}}
        outcome = graph.invoke({'answer':''} if mode=='interrupt-start' else Command(resume='synthetic answer'), graph_config, durability='sync')
        with sqlite3.connect(config['effects']) as effects:
            visits = effects.execute('SELECT COUNT(*) FROM visits').fetchone()[0]
        print(json.dumps({'interrupted':bool(outcome.get('__interrupt__')), 'answer':outcome.get('answer'), 'node_entries':visits}))
        assert not attempts


if __name__ == '__main__':
    main()
