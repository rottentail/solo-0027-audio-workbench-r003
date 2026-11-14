"""Own one durable workspace and close workers before releasing ownership."""
import fcntl
from pathlib import Path
from .catalog import Catalog
from .assets import AssetLibrary
from .projects import ProjectLibrary
from .render_queue import RenderQueue


class Workbench:
    def __init__(self, root, workers=2):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._owner = (self.root / '.owner.lock').open('a+b')
        self._closed = False
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._owner.close()
            raise RuntimeError('workspace is already open in another process') from exc
        try:
            self.catalog = Catalog(self.root)
            self.assets = AssetLibrary(self.catalog)
            self.projects = ProjectLibrary(self.catalog, self.assets)
            self.renders = RenderQueue(self.catalog, self.projects, self.assets, workers)
        except BaseException:
            self._owner.close()
            raise

    def close(self):
        if not self._closed:
            self.renders.close()
            self._owner.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
