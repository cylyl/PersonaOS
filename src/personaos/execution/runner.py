"""Worker runner — the kernel main loop.

Per VIKUNJA.md and ADR 0001, this is the kernel:

    while True:
        task = scheduler.claim_next(worker_id, lease_ttl=...)
        if not task:
            sleep(poll_interval)
            continue

        node = registry.get_node_for_worker(worker_id)
        worker = registry.get_worker(worker_id)

        decision = security.evaluate(
            actor=worker,
            action=task.action,
            resource=task.target,
            context=task.context,
        )
        if decision == DENY:
            workload.fail(task, reason=decision.reason)
            events.emit("policy.denied", task=task.id)
            continue
        if decision == REQUIRE_APPROVAL:
            approval.request(task)
            continue

        adapter = registry.get_runtime(node)
        checkpoint.load(task.checkpoint_ref)
        result = adapter.execute(task, checkpoint)

        checkpoint.write(task, result.state)
        workload.complete(task, result=result)
        events.emit("task.completed", task=task.id)

Security surrounds every action.

TODO(v0.1): implement the loop as an asyncio task. Handle SIGTERM cleanly
(finish current task, release lease, then exit).
"""
