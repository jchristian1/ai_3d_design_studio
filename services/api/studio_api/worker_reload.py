"""Asking a local worker to reload the code on disk.

WHY THIS EXISTS

A Python process holds the code it imported at startup. Ordinarily unremarkable — but
this worker executes design changes on behalf of a browser, so every symptom of a stale
process looks like a product bug rather than a stale process:

* a new material is missing, so the agent truthfully reports the material does not exist;
* a fix to the risk classifier has not landed, so the user is asked to approve the same
  harmless operation again;
* a change to the renderer does nothing at all.

That cost three rounds of "it changed but it looks the same", with the correct
implementation sitting on disk the whole time.

THE WORKER FIXES THIS ITSELF

``blender_worker.reload`` watches its own source and re-executes when it changes, while
idle. That is the real remedy, and it belongs there because the worker is the only party
that can see its own files: a worker is a separate machine by design, connected outbound,
and the control plane deliberately knows nothing about its filesystem. A test enforces
that this package cannot even import the worker.

This module is only the manual override — a reload the user can ask for directly, for a
worker that has been busy, or one started with auto-reload disabled.

The request travels as a file rather than as a link message, because adding a message
type means bumping the wire protocol: every message is validated with
``additionalProperties: false``, so a peer predating a new type rejects it. That is the
right cost for a product capability and the wrong one for developer tooling. The
convention lives in ``studio_contracts.worker_control``, which both sides may import.

It therefore only reaches a worker sharing this filesystem — which is exactly the case
that needs it. Reloading a remote worker is a deployment concern, not this.
"""

from __future__ import annotations

import logging

from studio_contracts.worker_control import request_worker_reload

logger = logging.getLogger("studio_api.worker_reload")


def request_reload() -> bool:
    """Ask the local worker to reload. True when the request was written."""
    try:
        path = request_worker_reload()
    except OSError as error:
        # The runtime directory is not writable from here, which is what happens when
        # the worker is on another machine. Reported as a clean failure rather than an
        # exception, because it is an expected configuration, not a bug.
        logger.warning("could not ask the worker to reload: %s", error)
        return False
    logger.info("asked the worker to reload (%s)", path.name)
    return True


__all__ = ["request_reload"]
