"""pytest configuration + shared fixtures.

TODO(v0.1): provide:
  - postgres_container: testcontainers[postgres] fixture, function-scoped
  - db_session: async session bound to the test container, rolled back per test
  - worker_registry: in-memory + DB-backed fixture for registry tests
  - workload_queue: empty task queue per test
  - fake_adapter: Adapter Protocol implementation that returns canned Results
  - sample_worker_profile, sample_node, sample_task: load from examples/

The Postgres fixture is non-negotiable for integration tests. No mocks on
the queue / checkpoint / lease paths — those are exactly what v0.1 has to
prove works. Mocking them is shipping the bug we're trying to prevent.
"""
