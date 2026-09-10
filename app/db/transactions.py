from contextvars import ContextVar
from functools import wraps

from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern


current_session = ContextVar("mongo_transaction_session", default=None)


class SessionCollection:
    def __init__(self, collection, session):
        self.collection = collection
        self.session = session

    def __getattr__(self, name):
        attribute = getattr(self.collection, name)
        if not callable(attribute):
            return attribute

        def call(*args, **kwargs):
            kwargs.setdefault("session", self.session)
            return attribute(*args, **kwargs)

        return call


class SessionDatabase:
    def __init__(self, database, session):
        self.database = database
        self.session = session

    def __getitem__(self, name):
        return SessionCollection(self.database[name], self.session)

    def __getattr__(self, name):
        if name in {"client", "name", "command", "list_collection_names"}:
            return getattr(self.database, name)
        return self[name]


def bind_session(database):
    session = current_session.get()
    return SessionDatabase(database, session) if session is not None else database


def run_transaction(callback):
    from app.db.mongodb_config import get_database

    if current_session.get() is not None:
        return callback()

    def execute(session):
        token = current_session.set(session)
        try:
            return callback()
        finally:
            current_session.reset(token)

    with get_database().client.start_session() as session:
        return session.with_transaction(
            execute,
            read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority"),
            max_commit_time_ms=10000,
        )


def transactional(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return run_transaction(lambda: function(*args, **kwargs))

    return wrapped
