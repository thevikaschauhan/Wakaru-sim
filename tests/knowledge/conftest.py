"""Disposable PostgreSQL cluster. No environment DSN or shared database accepted."""
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

PG = Path('/opt/homebrew/opt/postgresql@16/bin')
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='session')
def cluster(tmp_path_factory):
    folder = tmp_path_factory.mktemp('knowledge-pg')
    data = folder/'data'
    socket_folder = tempfile.TemporaryDirectory(prefix='wkpg-',dir='/private/tmp')
    socket = Path(socket_folder.name)
    env = {**os.environ, 'LC_ALL':'C', 'LANG':'C'}
    subprocess.run([str(PG/'initdb'),'-D',str(data),'-A','trust','--no-locale','--encoding=UTF8'],check=True,capture_output=True,env=env)
    subprocess.run([str(PG/'pg_ctl'),'-D',str(data),'-l',str(folder/'server.log'),'-o',f'-k {socket} -h "" -F','-w','start'],check=True,capture_output=True,env=env)
    try:
        yield {'host':str(socket), 'dbname':'postgres'}, folder
    finally:
        subprocess.run([str(PG/'pg_ctl'),'-D',str(data),'-m','fast','-w','stop'],check=True,capture_output=True,env=env)
        socket_folder.cleanup()


@pytest.fixture
def database(cluster):
    params, folder = cluster
    name = 'knowledge_'+uuid.uuid4().hex
    with psycopg.connect(**params,autocommit=True) as db:
        db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    dsn = psycopg.conninfo.make_conninfo(**{**params,'dbname':name})
    with psycopg.connect(dsn) as db:
        db.execute((ROOT/'cart_recovery/domain/migrations/001_knowledge.sql').read_text())
    try:
        yield dsn
    finally:
        with psycopg.connect(**params,autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
