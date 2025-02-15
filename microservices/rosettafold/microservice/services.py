import glob
import math
import multiprocessing
import os
import pathlib
import subprocess
import threading
from contextlib import contextmanager

import psutil

from microservice.api_models import RunRosettaFoldResponse
from microservice.loggers import logger


class RosettaService:
    _store = set()

    def __init__(self):
        self._rosettafold_directory = '/RoseTTAFold'

    @contextmanager
    @staticmethod
    def _track_job(job_id: str | None):
        if job_id:
            RosettaService._store.add(job_id)
        try:
            yield True
        finally:
            if job_id:
                RosettaService._store.remove(job_id)

    def is_job_running(self, job_id: str) -> bool:
        return job_id in RosettaService._store

    async def run_rosettafold(self, job_id: str, fasta: bytes | None, a3m: bytes | None) -> RunRosettaFoldResponse:
        try:
            with RosettaService._track_job(job_id):
                return await self._run_rosettafold(fasta, a3m)
        except:
            logger.rosetta_exception()
            return RunRosettaFoldResponse(
                errors=['Critical error. Open the issue on our github and attach the error log'],
                pdb_content=None)
        finally:
            self._cleanup_generated_files()

    def _cleanup_generated_files(self):
        t000 = glob.glob(os.path.join(self._rosettafold_directory, 't000*'))

        for file in t000:
            if os.path.isfile(file) and '.pdb' not in pathlib.Path(file).suffixes:
                os.remove(file)

    async def _run_rosettafold(self, fasta: bytes, a3m: bytes) -> RunRosettaFoldResponse:
        bfd_dir_path = os.path.join(self._rosettafold_directory, 'bfd')
        uniref_dir_path = os.path.join(self._rosettafold_directory, 'UniRef30_2020_06')
        pdb100_dir_path = os.path.join(self._rosettafold_directory, 'pdb100_2021Mar03')

        errors = []
        if not os.path.exists(bfd_dir_path):
            logger.bfd_directory_does_not_exist()
            errors.append('BFD directory does not exist. Check readme')

        if not os.path.exists(uniref_dir_path):
            logger.uniref_directory_does_not_exist()
            errors.append('Uniref directory does not exist. Check readme')

        if not os.path.exists(pdb100_dir_path):
            logger.pdb100_directory_does_not_exist()
            errors.append('PDB100 directory does not exist. Check readme')

        if errors:
            return RunRosettaFoldResponse(pdb_content=None, errors=errors)

        if a3m:
            with open(os.path.join(self._rosettafold_directory, 't000_.msa0.a3m'), 'wb') as f:
                f.write(a3m)
        else:
            with open(os.path.join(self._rosettafold_directory, 'input.fasta'), 'wb') as f:
                f.write(fasta)

        def read_stdout(pipe):
            for line in iter(pipe.readline, ''):
                logger.rosetta_stdout(line)
            pipe.close()

        def read_stderr(pipe):
            for line in iter(pipe.readline, ''):
                logger.rosetta_stderr(line)
            pipe.close()

        cpu_count = multiprocessing.cpu_count()
        gb = math.floor(psutil.virtual_memory().total / (1 << 30))
        script = f'./run_e2e_ver.sh input.fasta . {cpu_count} {gb}'
        process = subprocess.Popen(script, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=True,
                                   cwd=self._rosettafold_directory)

        stdout_thread = threading.Thread(target=read_stdout, args=(process.stdout,))
        stderr_thread = threading.Thread(target=read_stderr, args=(process.stderr,))

        stdout_thread.start()
        stderr_thread.start()

        stdout_thread.join()
        stderr_thread.join()

        return_code = process.wait()
        logger.rosetta_return_code(return_code)

        if os.path.exists(os.path.join(self._rosettafold_directory, 't000_.e2e.pdb')):
            with open(os.path.join(self._rosettafold_directory, 't000_.e2e.pdb'), 'r') as pdb:
                return RunRosettaFoldResponse(pdb_content=pdb.read(), errors=[])

        error_file_order = [
            os.path.join(self._rosettafold_directory, 'log/make_msa.stderr'),
            os.path.join(self._rosettafold_directory, 'log/make_ss.stderr'),
            os.path.join(self._rosettafold_directory, 'log/hhsearch.stderr'),
            os.path.join(self._rosettafold_directory, 'log/network.stderr')
        ]

        for error_file in reversed(error_file_order):
            if os.path.exists(error_file):
                with open(error_file, 'r') as e:
                    file_content = e.read()
                    logger.rosetta_error(file_content, error_file)
                    return RunRosettaFoldResponse(pdb_content=None, errors=[e.read()])

        logger.rosetta_fatal_error()
        return RunRosettaFoldResponse(pdb_content=None,
                                      errors=['Critical error. Open the issue on our github and attach the error log'])
