"""
External-service adapters.

Per docs/architecture/kite-design.md §3, this package is the only place in the
repository allowed to know upstream (kap-server) schemas, envelopes, and event
names. Everything outside this package consumes the normalized types defined
here.
"""
