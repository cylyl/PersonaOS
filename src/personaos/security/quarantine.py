"""Worker kill / quarantine.

v0.1 scope: flip worker.status to 'quarantined'. The scheduler respects the
flag — quarantined workers do not claim tasks. The worker.quarantined event
is appended to the log with reason + quarantined_by.

unquarantine() reverses the flag (status -> 'active') after manual review.

TODO(v0.1): implement quarantine(worker_id, reason, quarantined_by) and
unquarantine(worker_id, reason). Both call into registry/service.py to
update worker.status + emit the corresponding event.
"""
