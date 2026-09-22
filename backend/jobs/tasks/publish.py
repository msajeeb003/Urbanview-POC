"""Publish job (build plan v2): approved data -> serving tables -> vector tiles -> map.

Steps for a ``municipality_id``:
1. copy reviewer-approved planning values and geometry from staging to the serving tables;
2. rebuild vector tiles (tippecanoe -> PMTiles) and upload to object storage;
3. record a publish version and an audit entry (who, what, when);
4. invalidate caches.

Nothing reaches the public map except through this task.
"""

from __future__ import annotations

import logging

from jobs.celery_app import celery_app

log = logging.getLogger("urbanview.jobs.publish")


@celery_app.task(bind=True)
def publish_approved_data(self, municipality_id: str, actor_id: str | None = None) -> dict:
    log.info(
        "publish_approved_data requested",
        extra={
            "municipality_id": municipality_id,
            "actor_id": actor_id,
            "task_id": self.request.id,
        },
    )
    return {"status": "not_implemented", "municipality_id": municipality_id}
