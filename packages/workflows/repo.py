"""Entry point for the Dagster user-code gRPC server.

The platform's Dagster Helm values run this container with
`dagster api grpc --python-file /opt/dagster/app/repo.py`; the Dockerfile
copies this file to that path. It only has to expose `defs`.
"""

from workflows.definitions import defs

__all__ = ["defs"]
