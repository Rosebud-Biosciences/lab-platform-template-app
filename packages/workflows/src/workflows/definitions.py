"""The hello-world pipelines: tiny assets that write greeting rows.

Deliberately tiny — the point is the wiring, not the workload: assets read
DATABASE_URL exactly like the webapp does, so in a preview they write to that
preview's copy-on-write Neon branch and in prod to the real database.
"""

import os

import dagster as dg
import ray
from db.engine import get_engine
from db.models import Greeting
from sqlalchemy import func, select
from sqlalchemy.orm import Session


@dg.asset
def scheduled_greeting() -> dg.MaterializeResult:
    """Insert one greeting and report the running total."""
    with Session(get_engine()) as session:
        session.add(Greeting(name="dagster"))
        session.commit()
        total = session.scalar(select(func.count()).select_from(Greeting))

    return dg.MaterializeResult(metadata={"total_greetings": total})


@dg.asset
def ray_fanned_greetings() -> dg.MaterializeResult:
    """Fan work out over Ray, gather the results, write them back in one place.

    The shape to copy: Dagster owns orchestration and the database write; Ray
    owns the parallel compute. With RAY_ADDRESS set (e.g.
    ray://<prefix>kuberay-head-svc.ray.svc:10001) the tasks spread across the
    Ray cluster; unset, Ray runs entirely inside the run pod / your laptop, so
    the example needs no infrastructure.
    """
    ray.init(address=os.environ.get("RAY_ADDRESS"), ignore_reinit_error=True)
    try:

        @ray.remote
        def greet(worker: int) -> str:
            return f"ray-worker-{worker}"

        names = ray.get([greet.remote(i) for i in range(8)])
    finally:
        ray.shutdown()

    with Session(get_engine()) as session:
        session.add_all(Greeting(name=name) for name in names)
        session.commit()
        total = session.scalar(select(func.count()).select_from(Greeting))

    return dg.MaterializeResult(metadata={"fanned_out": len(names), "total_greetings": total})


class GreetingBatch(dg.Config):
    """The id window (after_id, through_id] a stream micro-batch covers."""

    after_id: int
    through_id: int


@dg.asset
def greetings_digest(config: GreetingBatch) -> dg.MaterializeResult:
    """Process one micro-batch of the greetings stream (read-only).

    Dagster's answer to streaming is a cursor sensor emitting micro-batch runs
    (see greeting_stream_sensor below). This asset only reads its window —
    writing back to the same table it streams from would have the sensor
    triggering on its own output forever.
    """
    with Session(get_engine()) as session:
        names = list(
            session.scalars(
                select(Greeting.name)
                .where(Greeting.id > config.after_id, Greeting.id <= config.through_id)
                .order_by(Greeting.id)
            )
        )

    return dg.MaterializeResult(
        metadata={
            "batch_size": len(names),
            "id_window": f"({config.after_id}, {config.through_id}]",
            "names": names[:20],
        }
    )


greet_job = dg.define_asset_job("greet_job", selection=[scheduled_greeting])

ray_fanout_job = dg.define_asset_job("ray_fanout_job", selection=[ray_fanned_greetings])

stream_digest_job = dg.define_asset_job("stream_digest_job", selection=[greetings_digest])


@dg.sensor(
    job=stream_digest_job,
    minimum_interval_seconds=30,
    default_status=dg.DefaultSensorStatus.STOPPED,  # opt in from the UI
)
def greeting_stream_sensor(
    context: dg.SensorEvaluationContext,
) -> dg.RunRequest | dg.SkipReason:
    """Turn the greetings table into a stream: one run per batch of new rows.

    The cursor (highest id already digested) lives in Dagster's sensor state,
    so ticks are cheap — one MAX() — and restarts resume where they left off.
    Try it live: toggle the sensor on, POST /greetings on the webapp, and a
    stream_digest_job run appears within the tick interval.
    """
    after_id = int(context.cursor) if context.cursor else 0

    with Session(get_engine()) as session:
        through_id = session.scalar(select(func.max(Greeting.id))) or 0

    if through_id <= after_id:
        return dg.SkipReason(f"no greetings past id {after_id}")

    context.update_cursor(str(through_id))
    return dg.RunRequest(
        run_key=f"greetings-{after_id}-{through_id}",  # dedupes retried ticks
        run_config=dg.RunConfig(
            ops={"greetings_digest": GreetingBatch(after_id=after_id, through_id=through_id)}
        ),
    )


daily_greeting = dg.ScheduleDefinition(
    job=greet_job,
    cron_schedule="0 9 * * *",
    default_status=dg.DefaultScheduleStatus.STOPPED,  # opt in from the UI
)

defs = dg.Definitions(
    assets=[scheduled_greeting, ray_fanned_greetings, greetings_digest],
    jobs=[greet_job, ray_fanout_job, stream_digest_job],
    schedules=[daily_greeting],
    sensors=[greeting_stream_sensor],
)
