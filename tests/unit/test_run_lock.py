import pytest

from job_scout.run_lock import RunAlreadyActive, RunLock


def test_run_lock_rejects_parallel_holder_and_releases(tmp_path):
    path = tmp_path / "scan.lock"
    with RunLock(path):
        with pytest.raises(RunAlreadyActive):
            with RunLock(path):
                pass
    with RunLock(path):
        assert path.exists()
