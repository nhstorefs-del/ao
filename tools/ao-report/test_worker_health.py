import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import worker_health


class WorkerHealthTests(unittest.TestCase):
    def test_three_same_failures_open_for_one_hour_then_close(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'health.json'
            t=datetime(2026,10,12,9,0,tzinfo=timezone.utc)
            self.assertFalse(worker_health.record('worker','signature',t,path)['opened'])
            self.assertFalse(worker_health.record('worker','signature',t+timedelta(minutes=5),path)['opened'])
            row=worker_health.record('worker','signature',t+timedelta(minutes=10),path)
            self.assertTrue(row['opened'])
            self.assertTrue(worker_health.is_open('worker',t+timedelta(minutes=69),path=path))
            self.assertFalse(worker_health.is_open('worker',t+timedelta(minutes=70),path=path))
            self.assertFalse(worker_health.is_open('other',t+timedelta(minutes=15),path=path))


if __name__=='__main__': unittest.main()
