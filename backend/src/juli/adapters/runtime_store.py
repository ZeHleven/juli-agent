"""Single SQLite transaction authority for durable dispatch and commit fencing."""
from uuid import uuid4
from pydantic import ValidationError
from juli.core.common import DomainError, digest, canonical_json
from juli.core.runtime import Checkpoint
from juli.core.tasks import TaskSnapshot, TaskState, TaskStatus, transition_task
from .sqlite_migrations import transaction


class RuntimeStore:
    def __init__(self, store, token, workspace_id, task_id):
        self.store, self._token = store, token
        self.workspace_id, self.task_id = workspace_id, task_id

    def _auth(self, db):
        access = self.store._authorize(db, self._token, self.workspace_id, write=True)
        record = self.store._task(db, self.workspace_id, self.task_id)
        if access.principal.principal_id != record['snapshot']['principal_id']:
            raise DomainError('runtime_owner_required', 'Only the task creator can execute or continue it.')
        return access, record

    def _event(self, db, kind):
        db.execute('INSERT INTO runtime_events(task_id,kind,created_at) VALUES (?,?,?)', (self.task_id, kind, self.store.clock()))

    def _transition(self, db, access, record, target, question=None):
        state = TaskState.model_validate(record['state'])
        new = transition_task(state, access=access, expected_version=state.version, target=TaskStatus(target), pending_question=question)
        db.execute('UPDATE tasks SET state_json=?,state_version=? WHERE id=?', (new.model_dump_json(), new.version, self.task_id))
        if target in {'failed', 'completed', 'cancelled'}:
            db.execute('DELETE FROM runtime_locks WHERE task_id=?', (self.task_id,))
        self._event(db, target)

    def _load(self, db, profile, policy):
        row = db.execute('SELECT * FROM runtime_runs WHERE task_id=?', (self.task_id,)).fetchone()
        if row is None:
            raise DomainError('runtime_not_started', 'Initialize this task with an explicit provider profile.')
        if row['profile'] != profile or row['policy_json'] != policy.persistence_json():
            raise DomainError('runtime_config_changed', 'Resume with the original deployment profile and policy.')
        return Checkpoint.model_validate_json(row['checkpoint_json'])

    def _save(self, db, checkpoint):
        checkpoint = Checkpoint.model_validate(checkpoint)
        db.execute('UPDATE runtime_runs SET checkpoint_json=? WHERE task_id=?', (checkpoint.model_dump_json(), self.task_id))

    def start(self, profile, policy, make_checkpoint):
        with transaction(self.store.path, write=True) as db:
            access, record = self._auth(db)
            if db.execute('SELECT 1 FROM runtime_runs WHERE task_id=?', (self.task_id,)).fetchone():
                self._load(db, profile, policy)
                return
            if record['state']['status'] != 'prepared':
                raise DomainError('task_not_prepared', 'A new runtime requires a prepared task.')
            snapshot, package, _ = self.store._model_context(db, self.workspace_id, self.task_id)
            if db.execute('SELECT 1 FROM runtime_locks WHERE conversation_id=?', (snapshot.conversation_id,)).fetchone():
                raise DomainError('conversation_busy', 'Complete or cancel the active task in this conversation first.')
            checkpoint = make_checkpoint(snapshot, package)
            db.execute('INSERT INTO runtime_runs VALUES (?,?,?,?)', (self.task_id, profile, policy.persistence_json(), checkpoint.model_dump_json()))
            db.execute('INSERT INTO runtime_locks VALUES (?,?)', (snapshot.conversation_id, self.task_id))
            self._transition(db, access, record, 'running')

    def reserve(self, profile, policy, prepare, *, expected_actions=None):
        with transaction(self.store.path, write=True) as db:
            access, record = self._auth(db)
            checkpoint = self._load(db, profile, policy)
            if expected_actions is not None and checkpoint.actions != expected_actions:
                raise DomainError('stale_runtime_version', 'Reload the runtime before dispatching another action.')
            if record['state']['status'] != 'running':
                return None
            active = db.execute("SELECT * FROM runtime_actions WHERE task_id=? AND state='reserved'", (self.task_id,)).fetchone()
            if active:
                if self.store.clock() < active['expires_at']:
                    raise DomainError('runtime_busy', 'An action already owns this task.')
                # Unknown external outcome: retain all reservations, fence late commit, never replay.
                db.execute("UPDATE runtime_actions SET state='unknown' WHERE id=?", (active['id'],))
                self._event(db, 'outcome_unknown')
                self._transition(db, access, record, 'failed')
                return None
            try:
                snapshot, package, _ = self.store._model_context(db, self.workspace_id, self.task_id)
                checkpoint, kind, payload, seconds = prepare(checkpoint, snapshot, package)
            except (DomainError, ValidationError):
                self._event(db, 'admission_rejected')
                self._transition(db, access, record, 'failed')
                return None
            action = uuid4().hex
            db.execute('INSERT INTO runtime_actions VALUES (?,?,?,?,?)', (action, self.task_id, kind, 'reserved', self.store.clock() + seconds))
            self._save(db, checkpoint)
            self._event(db, kind + '_reserved')
            return action, checkpoint, kind, payload, seconds, package

    def commit(self, action, profile, policy, finish):
        with transaction(self.store.path, write=True) as db:
            access, record = self._auth(db)
            row = db.execute('SELECT * FROM runtime_actions WHERE id=? AND task_id=?', (action, self.task_id)).fetchone()
            if row is None or row['state'] != 'reserved' or record['state']['status'] != 'running' or self.store.clock() >= row['expires_at']:
                raise DomainError('action_fenced', 'This action can no longer commit.')
            snapshot, package, _ = self.store._model_context(db, self.workspace_id, self.task_id)
            checkpoint = self._load(db, profile, policy)
            checkpoint, target, question = finish(checkpoint, snapshot, package)
            if checkpoint.clarification_draft is not None:
                if target != 'pending' or not question or not question.strip():
                    raise DomainError('invalid_clarification_candidate', 'A candidate requires a pending question.')
                # Same task/scenario/authorization/original-read boundary as a
                # final result, in the transaction that commits the question.
                self.store._validate_result(db, access, self.workspace_id, self.task_id, checkpoint.clarification_draft)
            if checkpoint.review and checkpoint.review.draft is not None:
                # Validate the whole selection before dispatch, without fabricating receipts.
                for ref in checkpoint.review.evidence_ids:
                    self.store._evidence(db, self.workspace_id, snapshot, ref)
                if checkpoint.review.phase in {'review', 'final'}:
                    # Entering review requires the same live original-read checks as a result.
                    self.store._validate_result(db, access, self.workspace_id, self.task_id, checkpoint.review.draft)
            if checkpoint.result is not None:
                self.store._validate_result(db, access, self.workspace_id, self.task_id, checkpoint.result)
            self._save(db, checkpoint)
            db.execute("UPDATE runtime_actions SET state='committed' WHERE id=?", (action,))
            self._event(db, row['kind'] + '_committed')
            if target:
                self._transition(db, access, record, target, question)

    def abandon(self, action):
        """Internal cleanup capability: only fail its own reserved action; no data is returned."""
        with transaction(self.store.path, write=True) as db:
            row = db.execute("SELECT 1 FROM runtime_actions WHERE id=? AND task_id=? AND state='reserved'", (action, self.task_id)).fetchone()
            if not row:
                return
            db.execute("UPDATE runtime_actions SET state='unknown' WHERE id=?", (action,))
            record = self.store._task(db, self.workspace_id, self.task_id)
            if record['state']['status'] == 'running':
                state = TaskState.model_validate(record['state'])
                failed = TaskState(task_id=state.task_id, workspace_id=state.workspace_id, version=state.version+1, status='failed')
                db.execute('UPDATE tasks SET state_json=?,state_version=? WHERE id=?', (failed.model_dump_json(), failed.version, self.task_id))
                db.execute('DELETE FROM runtime_locks WHERE task_id=?', (self.task_id,))
                self._event(db, 'failed')
            self._event(db, 'outcome_unknown')

    def answer(self, profile, policy, *, expected_version, request_key, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 16000 or not isinstance(request_key, str) or not 1 <= len(request_key) <= 160:
            raise DomainError('invalid_answer', 'Use a nonempty bounded answer and idempotency key.')
        request_hash = digest(canonical_json({'version': expected_version, 'text': text}).encode())
        with transaction(self.store.path, write=True) as db:
            access, record = self._auth(db)
            checkpoint = self._load(db, profile, policy)
            old = db.execute('SELECT request_hash FROM runtime_answers WHERE task_id=? AND request_key=?', (self.task_id, request_key)).fetchone()
            if old:
                if old[0] != request_hash:
                    raise DomainError('idempotency_conflict', 'The answer key already describes a different request.')
                return
            if record['state']['version'] != expected_version or record['state']['status'] != 'pending':
                raise DomainError('stale_task_version', 'Reload the current clarification before answering.')
            # Check bound resources even while pending.
            snapshot = TaskSnapshot.model_validate(record['snapshot'])
            for resource in snapshot.resources:
                current, _ = self.store._resource(db, self.workspace_id, resource.version_id)
                if current != resource:
                    raise DomainError('integrity_error', 'Pinned resource changed.')
            from juli.core.models import ModelMessage
            updated = checkpoint.model_dump()
            updated['clarification_draft'] = None
            updated['messages'] = (*checkpoint.messages, ModelMessage(role='user', content=canonical_json({'clarification_answer': text})))
            self._save(db, Checkpoint.model_validate(updated))
            db.execute('INSERT INTO runtime_answers VALUES (?,?,?)', (self.task_id, request_key, request_hash))
            self._transition(db, access, record, 'running')

    def view(self, *, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise DomainError('invalid_cursor', 'Use a nonnegative event cursor and limit 1–100.')
        with transaction(self.store.path) as db:
            access, record = self._auth(db)
            row = db.execute('SELECT checkpoint_json FROM runtime_runs WHERE task_id=?', (self.task_id,)).fetchone()
            checkpoint = Checkpoint.model_validate_json(row[0]) if row else None
            result = checkpoint.result if checkpoint else None
            if result:
                self.store._validate_result(db, access, self.workspace_id, self.task_id, result)
            events = [dict(r) for r in db.execute('SELECT id,kind,created_at FROM runtime_events WHERE task_id=? AND id>? ORDER BY id LIMIT ?', (self.task_id, after, limit))]
            return {'state': record['state'], 'usage': {k: getattr(checkpoint, k) for k in ('actions', 'model_calls', 'input_units', 'output_tokens', 'active_seconds')} if checkpoint else None,
                    'result': result.model_dump(mode='json') if result else None, 'events': events,
                    'next_cursor': events[-1]['id'] if events else after}
