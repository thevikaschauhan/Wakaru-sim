"""Assemble the same allowlisted source artifact used by the lean Docker image."""
from pathlib import Path
import shutil
import sys


def build(source, destination):
    source = Path(source).resolve()
    destination = Path(destination)
    files = (source / 'ops/runtime/lean-files.txt').read_text().splitlines()
    destination.mkdir(parents=True, exist_ok=False)
    for name in files:
        if not name or name.startswith('#'):
            continue
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('invalid runtime path')
        original = source / relative
        if not original.is_file() or original.is_symlink():
            raise ValueError('runtime source missing or linked: ' + name)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)


if __name__ == '__main__':
    build(sys.argv[1], sys.argv[2])
