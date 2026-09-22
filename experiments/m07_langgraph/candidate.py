"""Evaluation only: LangGraph schedules Juli actions but owns no business state.

Not imported by juli, not shipped in its wheel, and not an alternate production runtime.
"""
from typing import TypedDict


class DispatchState(TypedDict):
    expected_actions: int
    status: str


def build_graph(runner, *, checkpointer=None, loop=False, after_commit=None):
    from langgraph.graph import END, START, StateGraph

    def dispatch(state: DispatchState):
        # Never replace this with a fresh count read inside a replayed node:
        # the old intent must not silently become a new model action.
        outcome = runner.step(expected_actions=state['expected_actions'])
        if after_commit:
            after_commit()
        return {'expected_actions': outcome['usage']['actions'], 'status': outcome['state']['status']}

    graph = StateGraph(DispatchState)
    graph.add_node('dispatch', dispatch)
    graph.add_edge(START, 'dispatch')
    if loop:
        graph.add_conditional_edges('dispatch', lambda state: 'dispatch' if state['status'] == 'running' else END)
    else:
        graph.add_edge('dispatch', END)
    return graph.compile(checkpointer=checkpointer)


class GraphDriver:
    def __init__(self, runner):
        self.runner = runner

    def step(self, *, expected_actions):
        build_graph(self.runner).invoke({'expected_actions': expected_actions, 'status': ''}, {'recursion_limit': 110})
        return self.runner.view()

    def run(self):
        current = self.runner.view()
        if current['state']['status'] not in {'prepared', 'running'}:
            return current
        build_graph(self.runner, loop=True).invoke(
            {'expected_actions': current['usage']['actions'], 'status': current['state']['status']},
            {'recursion_limit': 110},
        )
        return self.runner.view()

    def answer(self, **kwargs):
        return self.runner.answer(**kwargs)

    def view(self):
        return self.runner.view()
