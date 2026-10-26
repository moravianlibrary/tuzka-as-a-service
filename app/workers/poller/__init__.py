"""The poller worker: polls engines for in-flight jobs, harvests finished ones (stores
their outputs) and fails or requeues the rest. ``worker`` holds the worker and its loop,
``steps`` the stateless steps, ``types`` the values passed between them."""
