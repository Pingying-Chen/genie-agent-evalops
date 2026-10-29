"""A workspace folder as the hand-off between two job identities (regression runs).

The SDK's workspace calls are synchronous; each runs in a thread so the event loop keeps going.
"""

import asyncio
import io

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.workspace import ImportFormat, ObjectType


class WorkspaceFolder:
    def __init__(self, w: WorkspaceClient, path: str):
        self._w = w
        self.path = path.rstrip("/")

    async def read(self, name: str) -> str:
        def download() -> str:
            with self._w.workspace.download(f"{self.path}/{name}") as f:
                return f.read().decode("utf-8")

        return await asyncio.to_thread(download)

    async def write(self, name: str, text: str) -> None:
        def upload() -> None:
            parent = f"{self.path}/{name}".rsplit("/", 1)[0]
            self._w.workspace.mkdirs(parent)
            self._w.workspace.upload(
                f"{self.path}/{name}",
                io.BytesIO(text.encode("utf-8")),
                format=ImportFormat.AUTO,
                overwrite=True,
            )

        await asyncio.to_thread(upload)

    async def names(self, prefix: str) -> list[str]:
        """Files directly under `<folder>/<prefix>`, as names relative to the folder."""

        def listing() -> list[str]:
            base = f"{self.path}/{prefix}".rstrip("/")
            try:
                objects = list(self._w.workspace.list(base))
            except Exception:  # NotFound: nothing written there yet
                return []
            return sorted(
                o.path[len(self.path) + 1 :] for o in objects if o.object_type == ObjectType.FILE and o.path
            )

        return await asyncio.to_thread(listing)
