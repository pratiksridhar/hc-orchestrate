"""Imported by every test module that runs git: keep the developer's git configuration out of the tests.

The tests make their own repositories and set user.name/user.email in them. A global or system setting
such as commit.gpgsign, core.hooksPath or user.useConfigOnly would otherwise decide whether their commits
(and the ones `hco merge` and hcboard make in those repositories) succeed. Set at import, before any test
copies os.environ for a subprocess or for the hc core of a live test.
"""
import os

os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
