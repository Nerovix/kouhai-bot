"""Repository modules grouped by domain. Callers pass a Database explicitly."""

from . import posts, problems, runtime, submissions, users

__all__ = ["posts", "problems", "runtime", "submissions", "users"]