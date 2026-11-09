"""Small platform adapters shared by playback and the local inspector."""
import os
import shutil
import signal
import subprocess


def terminate_tree(pid):
    """Kill a worker and its decoders, including when the worker is blocked."""
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, timeout=10)
        return
    result = subprocess.run(['ps', '-eo', 'pid=,ppid='], capture_output=True, text=True, check=True)
    children = {}
    for line in result.stdout.splitlines():
        child, parent = map(int, line.split())
        children.setdefault(parent, []).append(child)
    def kill_tree(current):
        for child in children.get(current, []):
            kill_tree(child)
        try:
            os.kill(current, signal.SIGKILL)
        except ProcessLookupError:
            pass
    kill_tree(pid)


def ffmpeg_executable():
    configured = os.environ.get('TV_FFMPEG')
    if configured:
        if not os.path.isfile(configured):
            raise FileNotFoundError('TV_FFMPEG does not point to a file')
        return configured
    installed = shutil.which('ffmpeg')
    if installed:
        return installed
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise RuntimeError('FFmpeg missing: run setup-debug.ps1 or install ffmpeg') from exc


def lock_experiment(file):
    if os.name == 'nt':
        import msvcrt
        file.seek(0, 2)
        if not file.tell():
            file.write('0')
            file.flush()
        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError('Experiment already locked') from exc
    else:
        import fcntl
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
