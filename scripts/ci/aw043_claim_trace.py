"""Observe real ownership accounting during the unchanged failing PTB test."""
import json

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    if "error_before_entry-cancel" not in item.nodeid:
        yield
        return
    from plugins.platforms.telegram.update_admission import TelegramApplication

    create = TelegramApplication._Application__create_task
    release = TelegramApplication._release_claim

    def observed_create(self, coroutine, update=None, is_error_handler=False, name=None):
        claim = self._current_claim.get()
        before = claim.owners if claim else None
        task = create(self, coroutine, update, is_error_handler, name)
        print("CLAIM_TRACE " + json.dumps({"event": "create", "task": id(task),
              "error_handler": is_error_handler, "owners_before": before,
              "owners_after": claim.owners if claim else None,
              "coroutine": getattr(getattr(coroutine, "cr_code", None), "co_name", "unknown")}))
        return task

    def observed_release(self, claim, task=None):
        print("CLAIM_TRACE " + json.dumps({"event": "release-before", "task": id(task) if task else None,
              "owners": claim.owners, "cancelled": task.cancelled() if task else None}))
        result = release(self, claim, task)
        print("CLAIM_TRACE " + json.dumps({"event": "release-after", "task": id(task) if task else None,
              "owners": claim.owners, "pending": claim.key in self.adapter._inflight_update_ids}))
        return result

    TelegramApplication._Application__create_task = observed_create
    TelegramApplication._release_claim = observed_release
    try:
        yield
    finally:
        TelegramApplication._Application__create_task = create
        TelegramApplication._release_claim = release
