"""The submit worker: dispatches queued jobs to healthy backends, scored by how soon each
would finish them. ``worker`` holds the worker and its loop, ``steps`` the allocation,
``types`` the values it allocates."""
