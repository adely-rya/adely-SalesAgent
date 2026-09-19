import os
import json
import subprocess
import sys
import pytest
from sqlalchemy import select
from app.database import init_database
from app.models import VCProfile


def environment(tmp_path):
    return dict(os.environ, OPENAI_API_KEY='hogehoge', DISCORD_WEBHOOK_URL='hogehoge',
                DATABASE_URL=f'sqlite:///{tmp_path / "sales.db"}')


def test_dummy_run_once(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'app.main', 'run-once'],
        env=environment(tmp_path), capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert 'OPENAI_API_KEY is not configured.' in result.stderr
    assert (tmp_path / 'sales.db').exists()


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux Docker signal handling')
def test_dummy_daemon_stays_alive_and_stops(tmp_path):
    process = subprocess.Popen([sys.executable, '-m', 'app.main', 'daemon'],
        env=environment(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            process.communicate(timeout=2)
        process.terminate()
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 0
        assert 'scheduler started' in stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_import_vc_profiles_command(tmp_path):
    source = tmp_path / 'profiles.json'
    source.write_text(json.dumps([{'name': 'Example Ventures', 'stage_focus': ['seed'],
                                   'creative_support_level': 2}]), encoding='utf-8')
    result = subprocess.run([sys.executable, '-m', 'app.main', 'import-vc-profiles', '--vc-profiles', str(source)],
        env=environment(tmp_path), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0
    factory = init_database(f'sqlite:///{tmp_path / "sales.db"}')
    with factory() as session:
        assert session.scalar(select(VCProfile)).name == 'Example Ventures'
    factory.kw['bind'].dispose()
