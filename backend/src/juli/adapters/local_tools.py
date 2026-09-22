"""Two reviewed local executors; not a loader for arbitrary model-named code."""
from juli.core.common import DomainError
from .sqlite_migrations import operation_deadline


class LocalEvidenceTools:
    def __init__(self, store, token: str, workspace_id: str, task_id: str):
        self.store = store
        self._token = token
        self.workspace_id = workspace_id
        self.task_id = task_id

    def execute(self, reference, arguments: dict, *, timeout_seconds: float) -> dict:
        if reference.version != '1.0.0':
            raise DomainError('tool_not_found', 'This executor version is not installed.')
        with operation_deadline(timeout_seconds):
            if reference.name == 'evidence.search':
                return self.store.search_evidence(self._token, self.workspace_id, self.task_id, arguments['query'])
            if reference.name == 'evidence.read':
                return self.store.read_evidence(self._token, self.workspace_id, self.task_id, arguments['evidence_id'])
        raise DomainError('tool_not_found', 'No reviewed executor is installed for this tool.')
