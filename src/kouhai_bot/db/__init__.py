"""SQLite state layer for the official-API multi-group bot."""

from .database import Database, get_database, set_database
from .schema import SCHEMA_VERSION

__all__ = ["Database", "get_database", "set_database", "SCHEMA_VERSION"]