"""Repository modules grouped by domain. Callers pass a Database explicitly."""

from . import feedback, posts, problems, runtime, submissions, users

__all__ = ["feedback", "posts", "problems", "runtime", "submissions", "users"]